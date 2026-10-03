"""Recorded macOS behaviour of the update transaction, and the host seam (#856).

The characterization tests below were written against `engine_update` *before*
the platform seam existed, and they are the proof that extracting it changed
nothing: the exact `launchctl` argv list, the exact plist documents and the
exact order of the record's phases, for every path through the transaction.
They are recorded from the implementation as it was, they pass on both sides of
the change, and they are not to be edited to accommodate one.
"""

from __future__ import annotations

import plistlib
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ciao import update_host
from ciao.engine_update import (
    PREVIOUS_ENV_NAME,
    UpdateError,
    apply_update,
    read_operation,
    recover_apply,
    recover_interrupted_apply,
    run_apply,
)
from ciao.install_receipt import read_receipt
from ciao.update_host import (
    MacUpdateHost,
    UnsupportedPlatformError,
    UpdateHost,
    current_update_host,
)
from tests.test_engine_update import (
    FROM_VERSION,
    TO_VERSION,
    _apply,
    _broken_probe,
    _env_version,
    _FakeClock,
    _handoff,
    _interrupted,
    _recover_apply,
    _run,
    _staged,
    _write_server_plist,
    phases,
)

UPDATER_PLIST = "com.ciao.updater.plist"
RECOVER_PLIST = "com.ciao.recover.plist"
UPDATER = "com.ciao.updater"
RECOVER = "com.ciao.recover"
SERVER = "com.ciao.server"
DOMAIN = "gui/501"


# ── apply_update: the foreground half, on the real macOS path ───────────


# MacUpdateHost: launchd's gui/<uid> domain (os.getuid), SIGHUP, and the POSIX
# shebang entry-point shims a macOS env carries. These tests patch
# sys.platform to darwin to drive it from any OS; on Windows those calls do not
# exist. The Windows update host (#857) carries its own tests.
mac_update_host = pytest.mark.skipif(
    sys.platform == "win32", reason="drives MacUpdateHost (launchd gui/<uid>, SIGHUP, shebang shims)"
)


def test_macos_apply_update_runs_exactly_these_launchctl_calls(tmp_path: Path) -> None:
    _, state, _, engine = _staged(tmp_path)

    result = _apply(engine, state)

    assert result.phase == "applying"
    # Two jobs, each booted out before it is bootstrapped, and nothing else: the
    # engine is still serving, so the transaction has touched no file.
    assert engine.launchctl_calls == [
        ["bootout", f"{DOMAIN}/{UPDATER}"],
        ["bootstrap", DOMAIN, str(state / UPDATER_PLIST)],
        ["bootout", f"{DOMAIN}/{RECOVER}"],
        ["bootstrap", DOMAIN, str(state / RECOVER_PLIST)],
    ]


def test_macos_apply_update_writes_the_updater_plist(tmp_path: Path) -> None:
    op, state, _, engine = _staged(tmp_path)

    _apply(engine, state)

    assert plistlib.loads((state / UPDATER_PLIST).read_bytes()) == {
        "Label": UPDATER,
        "ProgramArguments": [
            op.env_python,
            "-I",
            "-m",
            "ciao.engine_update",
            "run-apply",
            "--operation",
            op.id,
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(Path(op.stage_dir) / "updater.log"),
        "StandardErrorPath": str(Path(op.stage_dir) / "updater.log"),
    }


def test_macos_apply_update_writes_the_recover_plist(tmp_path: Path) -> None:
    op, state, _, engine = _staged(tmp_path)

    _apply(engine, state)

    # The same job on a timer, and from the staged interpreter: the net is only
    # worth anything if its program is an env the swap does not consume.
    assert plistlib.loads((state / RECOVER_PLIST).read_bytes()) == {
        "Label": RECOVER,
        "ProgramArguments": [
            op.env_python,
            "-I",
            "-m",
            "ciao.engine_update",
            "run-recover",
            "--operation",
            op.id,
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(Path(op.stage_dir) / "recover.log"),
        "StandardErrorPath": str(Path(op.stage_dir) / "recover.log"),
        "StartInterval": 30,
    }


def test_macos_apply_update_writes_the_record_phases_in_order(
    tmp_path: Path, phases: list[str]
) -> None:
    _, state, _, engine = _staged(tmp_path)

    _apply(engine, state)

    assert phases == ["draining", "applying"]


# ── run_apply: the detached half, in every outcome ──────────────────────


def test_macos_run_apply_success_runs_exactly_these_launchctl_calls(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "applied"
    assert engine.launchctl_calls == [
        # The pre-flight asks launchd what `com.ciao.server` really runs.
        ["print", f"{DOMAIN}/{SERVER}"],
        # The engine is out before a single file moves.
        ["bootout", f"{DOMAIN}/{SERVER}"],
        # The net is re-pointed at the env the swap just set aside.
        ["bootout", f"{DOMAIN}/{RECOVER}"],
        ["bootstrap", DOMAIN, str(state / RECOVER_PLIST)],
        # And retired on the way out, the bootout last.
        ["bootout", f"{DOMAIN}/{RECOVER}"],
    ]
    # The live env is the staged one, the old one is kept for a rollback, and
    # the receipt names the release that is now installed.
    assert (engine.live_env / "uv-receipt.toml").is_file()
    assert (Path(op.stage_dir) / "previous-env" / "bin" / "python").exists()
    assert receipt_path.read_text(encoding="utf-8").count(TO_VERSION) == 1


def test_macos_run_apply_success_writes_the_record_phases_in_order(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    _run(engine, op, state, receipt_path)

    assert phases == ["stopping", "swapping", "starting", "verifying_start", "applied"]


def test_macos_run_apply_rollback_runs_exactly_these_launchctl_calls(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    result = _run(engine, op, state, receipt_path, run=_broken_probe(engine))

    assert result.phase == "rolled_back"
    assert engine.launchctl_calls == [
        ["print", f"{DOMAIN}/{SERVER}"],
        ["bootout", f"{DOMAIN}/{SERVER}"],
        ["bootout", f"{DOMAIN}/{RECOVER}"],
        ["bootstrap", DOMAIN, str(state / RECOVER_PLIST)],
        # The rollback stops the engine again before it puts anything back.
        ["bootout", f"{DOMAIN}/{SERVER}"],
        ["bootout", f"{DOMAIN}/{RECOVER}"],
    ]
    # The install the operator was running is back, and they get a running
    # engine: a rollback that leaves either missing is the outcome it exists to
    # prevent.
    assert engine.up is True
    assert engine.starts == 1
    assert (engine.live_env / "bin" / "python").read_text(
        encoding="utf-8"
    ).splitlines()[1] == f"echo {FROM_VERSION}"


def test_macos_run_apply_rollback_writes_the_record_phases_in_order(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")

    _run(engine, op, state, receipt_path, run=_broken_probe(engine))

    assert phases == ["stopping", "swapping", "rolling_back", "rolled_back"]


def test_macos_run_apply_re_points_the_net_at_the_env_it_keeps(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    mid_swap: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        # Read where launchd reads it: the net's own document on disk, in the
        # gap the swap opens between the rename and the receipt rewrite.
        mid_swap.append(plistlib.loads((state / RECOVER_PLIST).read_bytes()))
        return _broken_probe(engine)(argv, **kwargs)

    _run(engine, op, state, receipt_path, run=run)

    assert [plist["ProgramArguments"][0] for plist in mid_swap] == [
        str(Path(op.stage_dir) / "previous-env" / "bin" / "python")
    ]


def test_macos_run_apply_preflight_refusal_touches_no_job_at_all(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The receipt the swap replaces is gone, so the pre-flight refuses on its
    # first question: not even launchd is asked, because there is no installed
    # env to compare a loaded job against.
    receipt_path.unlink()

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "no install receipt" in result.error
    assert engine.launchctl_calls == []
    assert engine.up is True
    assert phases == ["failed"]


def test_macos_run_apply_refuses_a_service_running_another_env(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    # The LaunchAgent on disk names an env the receipt knows nothing about, so
    # the pre-flight refuses — and the only launchctl call it makes is the
    # read-only question about what is loaded, which is never a `print` of the
    # plist: a job launchd loaded before the file was rewritten is the one
    # running.
    _write_server_plist(tmp_path / "elsewhere" / "ciaobot" / "bin" / "python")

    result = _run(engine, op, state, receipt_path)

    assert result.phase == "failed"
    assert "not the receipt's environment" in result.error
    assert engine.launchctl_calls == [["print", f"{DOMAIN}/{SERVER}"]]
    assert engine.up is True
    assert phases == ["failed"]


# ── recovery: the handoff and the job it bootstraps ─────────────────────


def test_macos_recover_interrupted_apply_runs_exactly_these_launchctl_calls(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")

    result = _handoff(engine, state)

    assert result is not None
    assert engine.launchctl_calls == [
        ["bootout", f"{DOMAIN}/{UPDATER}"],
        ["bootstrap", DOMAIN, str(state / UPDATER_PLIST)],
    ]
    # The same one-shot, in its recovery role, from the staged interpreter.
    assert plistlib.loads((state / UPDATER_PLIST).read_bytes()) == {
        "Label": UPDATER,
        "ProgramArguments": [
            op.env_python,
            "-I",
            "-m",
            "ciao.engine_update",
            "run-recover",
            "--operation",
            op.id,
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        "AbandonProcessGroup": True,
        "StandardOutPath": str(Path(op.stage_dir) / "updater.log"),
        "StandardErrorPath": str(Path(op.stage_dir) / "updater.log"),
    }


def test_macos_recover_apply_runs_exactly_these_launchctl_calls(tmp_path: Path) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")

    result = _recover_apply(engine, op, state, receipt_path)

    assert result is not None
    assert result.phase == "rolled_back"
    assert engine.launchctl_calls == [
        # Is a swap still in flight? A read-only question, asked before the lock
        # is trusted to be the only guard.
        ["print", f"{DOMAIN}/{UPDATER}"],
        ["bootout", f"{DOMAIN}/{SERVER}"],
        # And the net retires itself on the way out.
        ["bootout", f"{DOMAIN}/{RECOVER}"],
    ]
    assert engine.up is True
    assert engine.starts == 1


def test_macos_recover_apply_writes_the_record_phases_in_order(
    tmp_path: Path, phases: list[str]
) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")

    _recover_apply(engine, op, state, receipt_path)

    assert phases == ["rolling_back", "rolled_back"]


# ── the seam itself: selection, and what `MacUpdateHost` forwards ───────


@mac_update_host
def test_current_update_host_is_macos_on_darwin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")

    host = current_update_host()

    assert isinstance(host, MacUpdateHost)


@mac_update_host
def test_current_update_host_reads_platform_on_every_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys, "platform", "darwin")
    first = current_update_host()
    second = current_update_host()
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(UnsupportedPlatformError):
        current_update_host()

    # A fresh host each call, chosen from `sys.platform` as it is *now*: a
    # selector that cached its answer on first use would keep handing out the
    # macOS host on a machine that is not this one.
    assert isinstance(first, MacUpdateHost)
    assert first is not second


def test_current_update_host_is_windows_on_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao.windows_update import WindowsUpdateHost

    monkeypatch.setattr(sys, "platform", "win32")

    assert isinstance(current_update_host(), WindowsUpdateHost)
    assert isinstance(update_host.default_update_host(), WindowsUpdateHost)


def test_default_update_host_is_the_macos_host_off_windows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Linux CI drives the transaction through the macOS host and a fake
    # launchctl, as it did before Windows had a host: the caller's launchctl and
    # uid are the host's.
    calls: list[list[str]] = []

    def launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(sys, "platform", "linux")

    host = update_host.default_update_host(launchctl=launchctl, uid=777)

    assert isinstance(host, MacUpdateHost)
    host.stop_engine()
    assert calls == [["bootout", "gui/777/com.ciao.server"]]


@pytest.mark.parametrize("platform", ["linux", "cygwin", "freebsd14"])
def test_current_update_host_refuses_a_platform_with_no_host(
    platform: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", platform)

    with pytest.raises(UnsupportedPlatformError, match=platform) as excinfo:
        current_update_host()
    # The message names the platform, so an operator on one knows what is wrong.
    assert issubclass(UnsupportedPlatformError, RuntimeError)
    assert repr(platform) in str(excinfo.value)


def test_mac_update_host_is_an_update_host() -> None:
    # The Protocol is the contract the transaction is written against, and a
    # host that does not satisfy it would only fail on the platform it is used
    # on — so it is asserted here instead.
    host: UpdateHost = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    assert isinstance(host, MacUpdateHost)


def test_mac_update_host_forwards_launchctl_and_uid_exactly(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def launchctl(args: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "", "")

    host = MacUpdateHost(launchctl=launchctl, uid=777)
    state = tmp_path / "state"
    op = _operation(stage_dir=state / TO_VERSION)

    # A bootout of the engine, and a question about the updater job: two
    # different labels in the same `gui/<uid>` domain, both built from the two
    # arguments the caller injected rather than from `os.getuid()`.
    host.stop_engine()
    host.updater_running()
    host.swap_in_flight()
    host.spawn_updater(op, str(tmp_path / "env" / "bin" / "python"))
    host.retire_recovery_agent(state)

    assert calls == [
        ["bootout", "gui/777/com.ciao.server"],
        ["print", "gui/777/com.ciao.updater"],
        ["print", "gui/777/com.ciao.updater"],
        ["bootout", "gui/777/com.ciao.updater"],
        # The plist it wrote, named and in place: the job is bootstrapped from
        # the file, not from something held in memory.
        ["bootstrap", "gui/777", str(state / UPDATER_PLIST)],
        ["bootout", "gui/777/com.ciao.recover"],
    ]
    assert (state / UPDATER_PLIST).is_file()


def test_mac_update_host_stop_engine_runs_the_wait_it_is_given() -> None:
    # The port probe is the transaction's, not the host's, so the host is handed
    # it and answers with what it said — that is the whole contract of `wait`.
    waited: list[str] = []
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    assert host.stop_engine(wait=lambda: waited.append("waited") or True) is True
    assert waited == ["waited"]
    # No `wait` is what the rollback passes, and it asks for the stop alone.
    assert host.stop_engine() is True


@mac_update_host
def test_mac_update_host_interrupt_signals_are_term_and_hup() -> None:
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    # The two ways a ten-minute drain really ends besides Ctrl-C: the terminal
    # closing, and a supervisor stopping the process. `SIGHUP` is an
    # `AttributeError` on Windows, which is why the set comes from the host.
    assert host.interrupt_signals() == (signal.SIGTERM, signal.SIGHUP)


def test_mac_update_host_env_python_and_uv_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    assert host.env_python(tmp_path / "env") == tmp_path / "env" / "bin" / "python"
    assert host.uv_candidates() == [str(home / ".local" / "bin" / "uv")]


def test_mac_update_host_redirect_detached_stdio_writes_nothing(
    tmp_path: Path,
) -> None:
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    assert host.redirect_detached_stdio(tmp_path) is None
    # Each job's plist names its own log, so a no-op here is the whole answer.
    assert list(tmp_path.iterdir()) == []


def test_mac_update_host_after_env_restored_writes_nothing(tmp_path: Path) -> None:
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    # A macOS rollback renames the previous env back, and every absolute path
    # that named it named it before the swap too — so there is nothing to repair.
    assert host.after_env_restored(tmp_path / "env", bin_dir=tmp_path, wheel=tmp_path / "w.whl") is None


def test_mac_update_host_start_engine_is_the_service_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import macos_service

    seen: list[Path] = []

    def fake_start(**kwargs: Any) -> SimpleNamespace:
        seen.append(kwargs.get("runtime") or Path("no-runtime"))
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(macos_service, "start_service", fake_start)
    host = MacUpdateHost(launchctl=lambda args: subprocess.CompletedProcess(args, 0), uid=501)

    result = host.start_engine()

    # The same `ServiceResult` shape `_require_started` reads `.ok` from, from
    # the helper that already owns starting the engine.
    assert result.ok is True
    assert seen == [Path("no-runtime")]


# ── a FakeHost: the ordering and rollback decisions part (b) reuses ─────


class FakeHost:
    """An `UpdateHost` that records the transaction's calls, in order.

    Nothing here touches launchd, a supervisor or a real env swap: the point is
    the *order* the transaction asks for, and the decision it makes when one of
    the answers is a refusal. The methods that would have to move real files do
    the real thing through `MacUpdateHost` — this is a seam test, not a
    filesystem test — and everything else is recorded and answers something a
    test chose.
    """

    def __init__(
        self,
        engine: Any,
        *,
        install_error: Exception | None = None,
        stop_sticks: bool = False,
        updater_error: Exception | None = None,
        program: str | None = None,
        refuse_restore: bool = False,
    ) -> None:
        self.engine = engine
        self.calls: list[tuple[str, ...]] = []
        self.install_error = install_error
        self.stop_sticks = stop_sticks
        self.updater_error = updater_error
        self.program = program
        # The one move a rollback cannot survive losing, so a test can lose it
        # without mocking the filesystem under the transaction's feet.
        self.refuse_restore = refuse_restore
        self._mac = MacUpdateHost(
            launchctl=lambda args: subprocess.CompletedProcess(args, 0, "", ""),
            uid=501,
        )

    def _record(self, name: str, *rest: object) -> None:
        self.calls.append((name, *(str(item) for item in rest)))

    def engine_port(self) -> int:
        self._record("engine_port")
        return 8443

    def start_engine(self) -> Any:
        self._record("start_engine")
        return self.engine.start_service()

    def stop_engine(self, wait: Any = None) -> bool:
        self._record("stop_engine")
        if not self.stop_sticks:
            self.engine.up = False
        # `stop_sticks` is a stop the engine refuses to honour: it keeps
        # answering, so the transaction's own port probe — not this method — is
        # what reports that it did not stop.
        return True if wait is None else bool(wait())

    def server_program(self) -> str | None:
        self._record("server_program")
        return self.program

    def spawn_updater(
        self, op: Any, python: str, *, verb: str = "run-apply", args: Any = None
    ) -> None:
        self._record("spawn_updater", verb, python)
        if self.updater_error is not None:
            raise self.updater_error

    def install_recovery_agent(self, op: Any, python: str, root: Path) -> Path:
        self._record("install_recovery_agent", python)
        return root / update_host.RECOVER_PLIST_NAME

    def recovery_python(self, op: Any, previous_env: Path) -> str:
        self._record("recovery_python")
        return op.env_python or self._mac.env_python(previous_env).as_posix()

    def retire_recovery_agent(self, root: Path) -> None:
        self._record("retire_recovery_agent")

    def updater_running(self) -> bool:
        self._record("updater_running")
        return False

    def swap_in_flight(self) -> bool:
        self._record("swap_in_flight")
        return False

    def move_env(self, source: Path, dest: Path) -> None:
        self._record("move_env", source.name, dest.name)
        if self.refuse_restore and source.name == PREVIOUS_ENV_NAME:
            raise OSError(13, "permission denied")
        self._mac.move_env(source, dest)

    def env_python(self, env: Path) -> Path:
        self._record("env_python", env.name)
        return self._mac.env_python(env)

    def find_staged_python(self, tool_dir: Path) -> Path:
        self._record("find_staged_python")
        raise AssertionError("staging is not exercised by these host tests")

    def install_env(
        self, op: Any, staged: Path, live: Path, *, bin_dir: Path, wheel: Path
    ) -> None:
        self._record("install_env")
        if self.install_error is not None:
            raise self.install_error
        self._mac.install_env(op, staged, live, bin_dir=bin_dir, wheel=wheel)

    def after_env_restored(self, live: Path, *, bin_dir: Path, wheel: Path) -> None:
        self._record("after_env_restored", live.name)

    def uv_candidates(self) -> list[str]:
        return self._mac.uv_candidates()

    def interrupt_signals(self) -> tuple[signal.Signals, ...]:
        return self._mac.interrupt_signals()

    def redirect_detached_stdio(self, root: Path) -> None:
        self._record("redirect_detached_stdio")


def _operation(stage_dir: Path | None = None) -> Any:
    """A record with the fields the host reads, and a stage dir of its own.

    The stage dir is what the host derives the update state dir from (its
    parent), so a test that writes a plist has to point it somewhere real.
    """
    from ciao.engine_update import Operation

    return Operation(
        id="20260925T100000-1.2.3",
        phase="applying",
        from_version=FROM_VERSION,
        to_version=TO_VERSION,
        started_at="2026-09-25T10:00:00+00:00",
        updated_at="2026-09-25T10:00:00+00:00",
        stage_dir=str(stage_dir or Path("/state") / TO_VERSION),
    )


def _run_with_host(
    engine: Any, op: Any, state: Path, receipt_path: Path, host: Any, **kwargs: Any
) -> Any:
    """``run_apply`` with the host as its *only* platform seam.

    Deliberately not the shared `_run` helper: that one also injects
    ``launchctl``, ``uid`` and ``start_service``, which would be a second seam
    answering the same questions the host is here to answer, and the recorded
    order would no longer be the transaction's.
    """
    return run_apply(
        op.id,
        state_dir=state,
        http_post=engine.post,
        http_get=engine.get,
        run=engine.probe_run,
        sleep=lambda seconds: None,
        clock=_FakeClock(),
        receipt_path=receipt_path,
        host=host,
        **kwargs,
    )


def _apply_with_host(engine: Any, state: Path, host: Any, **kwargs: Any) -> Any:
    """``apply_update`` with the host as its only platform seam."""
    return apply_update(
        state_dir=state,
        http_post=engine.post,
        sleep=lambda seconds: None,
        clock=_FakeClock(),
        host=host,
        **kwargs,
    )


def _recover_with_host(
    engine: Any, op: Any, state: Path, receipt_path: Path, host: Any, **kwargs: Any
) -> Any:
    """``recover_apply`` with the host as its only platform seam."""
    return recover_apply(
        op.id,
        state_dir=state,
        http_post=kwargs.pop(
            "http_post", lambda url: pytest.fail("recovery must post nothing")
        ),
        http_get=engine.get,
        sleep=lambda seconds: None,
        clock=_FakeClock(),
        receipt_path=receipt_path,
        host=host,
        **kwargs,
    )


def test_fake_host_run_apply_preflight_refusal_never_stops_the_engine(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(engine, program=engine.live_env / "bin" / "python")
    # The receipt the swap replaces is gone, so the pre-flight refuses on its
    # first question.
    receipt_path.unlink()

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "failed"
    # The receipt is the first question, so the host is asked for the port (to
    # build the base URL) and nothing else: without an installed env there is
    # nothing to compare a loaded service against. A refusal before the stop is
    # the whole answer — nothing to roll back.
    assert [call[0] for call in host.calls] == ["engine_port"]


def test_fake_host_run_apply_refuses_a_service_running_another_env(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    elsewhere = tmp_path / "elsewhere" / "env" / "bin" / "python"
    host = FakeHost(engine, program=elsewhere.as_posix())

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "failed"
    assert "not the receipt's environment" in result.error
    assert [call[0] for call in host.calls] == ["engine_port", "server_program"]
    assert engine.up is True


def test_fake_host_run_apply_stops_once_and_moves_nothing_when_it_will_not_stop(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(engine, stop_sticks=True)

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "failed"
    assert "did not stop" in result.error
    # Stopped once, start attempted once, and no file touched: nothing was
    # swapped, so there is nothing to roll back and nothing to restore.
    assert [call[0] for call in host.calls] == [
        "engine_port",
        "server_program",
        "stop_engine",
        "start_engine",
    ]
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()


def test_fake_host_run_apply_rolls_back_through_the_host_in_order(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(
        engine, install_error=UpdateError("the env could not be placed")
    )

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "rolled_back", result.error
    assert "the env could not be placed" in result.error
    # The whole ordering, through the seam: ask what the service runs, stop it,
    # re-point the net, set the old env aside, install, then — because the
    # install failed — stop again, put the old env back, re-point the entry
    # points, and start. The engine is started last and unconditionally.
    assert [call[0] for call in host.calls] == [
        "engine_port",
        "server_program",
        "stop_engine",
        "move_env",
        "env_python",
        "install_recovery_agent",
        "install_env",
        "stop_engine",
        "move_env",
        "after_env_restored",
        "start_engine",
        "retire_recovery_agent",
    ]
    # And the operator has their engine: the old env back, the receipt restored,
    # and a running server reporting the version it is on.
    assert engine.up is True
    assert _env_version(engine.live_env) == FROM_VERSION
    assert read_receipt(receipt_path) is not None


def test_fake_host_run_apply_still_starts_the_engine_when_the_rollback_fails(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(engine, install_error=UpdateError("boom"))
    host.refuse_restore = True

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "rollback_failed"
    # Both reasons, and the start attempted anyway: a stopped engine is a worse
    # outcome than a broken one, and it is the last thing the rollback does.
    assert "boom" in result.error
    assert "restore the previous env" in result.error
    # A start is attempted whatever else failed, and the engine ends up running.
    # The net is *not* retired: the previous env is still on disk, so the
    # restore did not happen and the next recovery tick retries it.
    names = [call[0] for call in host.calls]
    assert names[-1] == "start_engine"
    assert "retire_recovery_agent" not in names
    assert (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert engine.starts == 1


def test_fake_host_run_apply_asks_the_host_for_the_whole_forward_path(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(engine)

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "applied"
    # Every host call the successful path makes, in order — the table part (b)
    # has to satisfy, recorded once here so both can be checked against it.
    assert [call[0] for call in host.calls] == [
        "engine_port",
        "server_program",
        "stop_engine",
        "move_env",
        "env_python",
        "install_recovery_agent",
        "install_env",
        "env_python",
        "start_engine",
        "retire_recovery_agent",
    ]


def test_fake_host_apply_update_hands_the_swap_to_the_host(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _staged(tmp_path)
    host = FakeHost(engine)

    result = _apply_with_host(engine, state, host)

    assert result.phase == "applying"
    assert [call[0] for call in host.calls] == [
        "engine_port",
        "spawn_updater",
        "install_recovery_agent",
    ]
    # The updater runs from the staged env, and the net is installed behind it
    # from the same interpreter: the swap is handed over before anything is
    # stopped, and both jobs name an env the swap has not consumed.
    assert host.calls[1][2] == op.env_python
    assert host.calls[2][1] == op.env_python


def test_fake_host_apply_update_reopens_admission_when_the_handoff_fails(
    tmp_path: Path,
) -> None:
    _, state, _, engine = _staged(tmp_path)
    host = FakeHost(engine, updater_error=UpdateError("the job would not start"))

    with pytest.raises(UpdateError, match="the job would not start"):
        _apply_with_host(engine, state, host)

    record = read_operation(state)
    assert record is not None
    assert record.phase == "failed"
    assert "the job would not start" in record.error
    # The drain was closed by the foreground half and nothing else reopens it.
    assert engine.posts[-1].endswith("/api/admin/drain/cancel")
    # The net is never installed behind a swap that was not handed over, and the
    # engine was never stopped: `fail()` reopens admission and returns.
    assert [call[0] for call in host.calls] == ["engine_port", "spawn_updater"]


def test_fake_host_recover_interrupted_apply_asks_the_host(tmp_path: Path) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")
    host = FakeHost(engine)

    result = recover_interrupted_apply(state_dir=state, uid=501, host=host)

    assert result is not None
    assert result.phase == "swapping"
    assert [call[0] for call in host.calls] == [
        "updater_running",
        "recovery_python",
        "spawn_updater",
    ]
    # In its recovery role, and from an interpreter that exists.
    assert host.calls[-1][1] == "run-recover"
    assert Path(host.calls[-1][2]).exists()


def test_fake_host_recover_interrupted_apply_defers_to_a_running_swap(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")

    class _Running(FakeHost):
        def updater_running(self) -> bool:
            self._record("updater_running")
            return True

    host = _Running(engine)

    result = recover_interrupted_apply(state_dir=state, uid=501, host=host)

    # The record belongs to the swap in flight: not rewritten, nothing started.
    assert result is not None
    assert result.phase == "swapping"
    assert [call[0] for call in host.calls] == ["updater_running"]


def test_fake_host_recover_interrupted_apply_records_a_job_that_will_not_start(
    tmp_path: Path,
) -> None:
    op, state, _, engine = _interrupted(tmp_path, "swapping")
    host = FakeHost(engine, updater_error=UpdateError("denied"))

    result = recover_interrupted_apply(state_dir=state, uid=501, host=host)

    # The phase stays an interrupted swap, so the next boot tries again, and the
    # reason is on the record: a record that says nothing leaves an operator
    # guessing why nothing happened.
    assert result is not None
    assert result.phase == "swapping"
    assert "recovery job did not start" in result.error
    assert "denied" in result.error
    assert read_operation(state) == result


def test_fake_host_recover_apply_rolls_back_through_the_host(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")
    host = FakeHost(engine)

    result = _recover_with_host(engine, op, state, receipt_path, host)

    assert result is not None
    assert result.phase == "rolled_back"
    # The same order the forward path's rollback uses, and the engine is
    # started last.
    assert [call[0] for call in host.calls] == [
        "engine_port",
        "swap_in_flight",
        "stop_engine",
        "move_env",
        "after_env_restored",
        "start_engine",
        "retire_recovery_agent",
    ]
    assert engine.up is True
    assert _env_version(engine.live_env) == FROM_VERSION


def test_fake_host_recover_apply_stands_down_while_a_swap_is_in_flight(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _interrupted(tmp_path, "swapping")

    class _InFlight(FakeHost):
        def swap_in_flight(self) -> bool:
            self._record("swap_in_flight")
            return True

    host = _InFlight(engine)

    result = _recover_with_host(engine, op, state, receipt_path, host)

    # Nothing rolled back over a swap that is still running: that is the race
    # the durable agent exists beside, not inside. The net also stays, because
    # this tick is the normal case and the next one is seconds away.
    assert result is None
    assert [call[0] for call in host.calls] == ["engine_port", "swap_in_flight"]
    assert engine.up is True


def test_fake_host_run_apply_refuses_a_wheel_that_no_longer_matches_its_digest(
    tmp_path: Path,
) -> None:
    op, state, receipt_path, engine = _staged(tmp_path, phase="applying")
    host = FakeHost(engine, program=engine.live_env / "bin" / "python")
    # The wheel changed on disk after staging verified it. A host that rebuilds
    # the env installs from this file (Windows, #857), so the check is shared
    # and runs before the engine is stopped.
    with Path(op.wheel).open("ab") as handle:
        handle.write(b"tampered")

    result = _run_with_host(engine, op, state, receipt_path, host)

    assert result.phase == "failed"
    assert "no longer matches the digest" in result.error
    assert "stop_engine" not in [call[0] for call in host.calls]
    assert engine.up is True
