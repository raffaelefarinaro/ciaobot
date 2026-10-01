"""ciao.windows_update: the Windows update host, on any OS, through fakes (#857).

What these pin is what the host asks Task Scheduler and uv to do, in what
order, and what the shared transaction in :mod:`ciao.engine_update` decides
from the answers. Nothing here runs ``schtasks`` or ``uv``: a recording
runner stands in for both, and the "environments" are directories whose
``Scripts/python.exe`` is a text file naming a version. The engine's
``server.lock`` is a real file locked through :mod:`ciao.os_support.locks`, so
the stop confirmation is exercised for real on every platform.
"""

from __future__ import annotations

import hashlib
import subprocess
import sys
import zipfile
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace
from typing import Any
from xml.etree import ElementTree

import pytest

from ciao import windows_service as ws
from ciao.engine_update import (
    PREVIOUS_ENV_NAME,
    Operation,
    UpdateError,
    read_operation,
    recover_apply,
    run_apply,
    write_operation,
)
from ciao.install_receipt import InstallReceipt, read_receipt, write_receipt
from ciao.os_support.locks import lock_exclusive
from ciao.update_host import UpdateHost
from ciao.windows_update import (
    CONSTRAINTS_NAME,
    DISCARDED_DIR_NAME,
    RECOVER_FILE_NAME,
    UPDATER_FILE_NAME,
    WindowsUpdateHost,
    _pins,
)

FROM_VERSION = "1.2.2"
TO_VERSION = "1.2.3"
PORT = 8765
ENTRY_POINTS = ("ciao", "ciaobot")
USER = r"DESKTOP-1\ada"
NS = ws.TASK_NS
FREEZE = "anyio==4.4.0\nciaobot @ file:///C:/stage/ciaobot-1.2.3-py3-none-any.whl\nhttpx==0.27.0\n"


# ── fixtures: a Windows install, laid out the way uv leaves it ──────────


def _write_env(env: Path, version: str) -> Path:
    """A stand-in tool env whose interpreter "reports" ``version``."""
    scripts = env / "Scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "python.exe").write_text(f"version {version}\n", encoding="utf-8")
    (scripts / "pythonw.exe").write_text(f"version {version}\n", encoding="utf-8")
    for name in ENTRY_POINTS:
        (scripts / f"{name}.exe").write_text(f"launcher {name} {version} {env}\n", encoding="utf-8")
    (env / "uv-receipt.toml").write_text("[tool]\n", encoding="utf-8")
    return scripts / "python.exe"


def _env_version(env: Path) -> str:
    try:
        return (env / "Scripts" / "python.exe").read_text(encoding="utf-8").split()[1]
    except (OSError, IndexError):
        return ""


def _wheel(path: Path, version: str) -> Path:
    dist_info = f"ciaobot-{version}.dist-info"
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{dist_info}/METADATA", f"Name: ciaobot\nVersion: {version}\n")
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\n" + "".join(f"{name} = ciao.cli:main\n" for name in ENTRY_POINTS),
        )
    return path


class _Machine:
    """The engine, Task Scheduler and uv, as the host sees them.

    ``schtasks`` records every argv and plays the engine task: ``/End`` stops
    the engine and ``/Run`` starts it. ``run`` plays uv (an install writes the
    target version into the live env, unless ``uv_error`` is set) and the
    interpreter's version probe. ``get`` is ``/api/startup-status``.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / ".env").write_text(f"PWA_PORT={PORT}\n", encoding="utf-8")
        self.task_dir = root / "service"
        self.task_dir.mkdir()
        (self.task_dir / ws.TASK_FILE_NAME).write_bytes(
            ws.render_task_xml(
                python=r"C:\uv\tools\ciaobot\Scripts\pythonw.exe",
                workspace=r"C:\placeholder",
                user=USER,
            )
            .replace(r"C:\placeholder", str(self.workspace))
            .encode("utf-16")
        )
        self.tools = root / "tools"
        self.live_env = self.tools / "ciaobot"
        self.bin_dir = root / "bin"
        self.bin_dir.mkdir()
        self.up = True
        self.starts = 0
        self.schtasks_calls: list[list[str]] = []
        self.uv_calls: list[tuple[list[str], dict[str, Any]]] = []
        self.uv_error: str | None = None
        self.task_program: str | None = None
        self.held_lock: Any = None

    # -- Task Scheduler ---------------------------------------------------
    def schtasks(self, *args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        argv = list(args)
        self.schtasks_calls.append(argv)
        if argv[:3] == ["/End", "/TN", ws.TASK_NAME]:
            self.up = False
            self.release_lock()
        elif argv[:3] == ["/Run", "/TN", ws.TASK_NAME]:
            self.starts += 1
            self.up = True
        elif argv[:1] == ["/Query"] and "/XML" in argv:
            if self.task_program is None:
                return subprocess.CompletedProcess(argv, 1, "", "ERROR: no such task")
            xml = (
                '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
                f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions>'
                f"<Exec><Command>{self.task_program}</Command></Exec></Actions></Task>\r\r\n"
            )
            return subprocess.CompletedProcess(argv, 0, xml, "")
        return subprocess.CompletedProcess(argv, 0, "SUCCESS", "")

    def changing_calls(self) -> list[list[str]]:
        return [call for call in self.schtasks_calls if call[0] != "/Query"]

    # -- the engine's runtime lock ---------------------------------------
    @property
    def server_lock(self) -> Path:
        return self.workspace / ".runtime" / "server.lock"

    def hold_lock(self) -> None:
        self.server_lock.parent.mkdir(parents=True, exist_ok=True)
        self.held_lock = self.server_lock.open("a+")
        lock_exclusive(self.held_lock.fileno(), blocking=False)

    def release_lock(self) -> None:
        if self.held_lock is not None:
            self.held_lock.close()
            self.held_lock = None

    # -- uv and the interpreter -------------------------------------------
    def run(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-c" in argv:
            return subprocess.CompletedProcess(argv, 0, f"{_env_version(Path(argv[0]).parent.parent)}\n", "")
        self.uv_calls.append((list(argv), kwargs))
        if self.uv_error is not None:
            raise subprocess.CalledProcessError(2, argv, output="", stderr=self.uv_error)
        env = kwargs["env"]
        live = Path(env["UV_TOOL_DIR"]) / "ciaobot"
        _write_env(live, TO_VERSION)
        for name in ENTRY_POINTS:
            (Path(env["UV_TOOL_BIN_DIR"]) / f"{name}.exe").write_bytes(
                (live / "Scripts" / f"{name}.exe").read_bytes()
            )
        return subprocess.CompletedProcess(argv, 0, "", "")

    # -- the engine's HTTP surface ----------------------------------------
    def get(self, url: str) -> dict[str, Any] | None:
        if not self.up:
            return None
        return {"version": _env_version(self.live_env), "overall_ready": True}

    def post(self, url: str) -> dict[str, Any]:
        return {"draining": True, "active_chat_ids": []}

    def start_service(self) -> SimpleNamespace:
        self.schtasks("/Run", "/TN", ws.TASK_NAME)
        return SimpleNamespace(ok=True)


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def _host(machine: _Machine, state: Path) -> WindowsUpdateHost:
    return WindowsUpdateHost(
        schtasks=machine.schtasks,
        run=machine.run,
        state_dir=state,
        task_dir=machine.task_dir,
        sleep=lambda seconds: None,
        clock=_Clock(),
    )


@pytest.fixture(autouse=True)
def _tmp_paths_are_task_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the task renderer take this OS's absolute tmp paths.

    The host renders task XML from real directories under ``tmp_path``, which on
    macOS and Linux are ``/tmp/...``, not drive-letter paths. The renderer's own
    Windows-path refusal is pinned with literal Windows paths in
    ``tests/test_windows_service.py``; here only "absolute on this OS" is kept.
    """
    if sys.platform == "win32":
        return

    def absolute(label: str, value: str) -> str:
        # `windowless_python` spells a POSIX /tmp path the Windows way (`\tmp\...`):
        # rooted, with no drive. Any of the three is an absolute path here.
        windows = PureWindowsPath(value)
        if not (Path(value).is_absolute() or windows.drive or windows.root):
            raise ValueError(f"{label} must be absolute: {value!r}")
        return ws._xml_text(label, value)

    monkeypatch.setattr(ws, "_windows_absolute", absolute)


@pytest.fixture
def user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERNAME", "ada")
    monkeypatch.setenv("USERDOMAIN", "DESKTOP-1")


def _staged(root: Path, *, phase: str = "applying") -> tuple[Operation, Path, Path, _Machine]:
    """A staged update, a Windows install of the previous release, and its receipt."""
    machine = _Machine(root)
    state = root / "updates"
    stage = state / TO_VERSION
    staged_python = _write_env(stage / "tool" / "ciaobot", TO_VERSION)
    wheel = _wheel(stage / f"ciaobot-{TO_VERSION}-py3-none-any.whl", TO_VERSION)
    live_python = _write_env(machine.live_env, FROM_VERSION)
    for name in ENTRY_POINTS:
        (machine.bin_dir / f"{name}.exe").write_bytes(
            (machine.live_env / "Scripts" / f"{name}.exe").read_bytes()
        )
    receipt_path = root / "install-receipt.json"
    receipt = InstallReceipt(
        version=FROM_VERSION,
        executable=str(machine.bin_dir / "ciao.exe"),
        python=str(live_python),
        service_backend="windows-task",
        service_label=ws.TASK_NAME,
        installed_at="2026-10-01T16:00:00+00:00",
        uv=str(root / "uv.exe"),
    )
    write_receipt(receipt, receipt_path)
    previous_receipt = stage / "previous-receipt.json"
    previous_receipt.write_bytes(receipt_path.read_bytes())
    op = Operation(
        id=f"20261001T100000-{TO_VERSION}",
        phase=phase,
        from_version=FROM_VERSION,
        to_version=TO_VERSION,
        started_at="2026-10-01T10:00:00+00:00",
        updated_at="2026-10-01T10:00:00+00:00",
        stage_dir=str(stage),
        wheel=str(wheel),
        wheel_sha256=hashlib.sha256(wheel.read_bytes()).hexdigest(),
        env_python=str(staged_python),
        env_freeze=FREEZE,
        previous_receipt=str(previous_receipt),
    )
    write_operation(op, state)
    machine.task_program = str(machine.live_env / "Scripts" / "pythonw.exe")
    return op, state, receipt_path, machine


def _run(op: Operation, state: Path, receipt_path: Path, machine: _Machine) -> Operation:
    return run_apply(
        op.id,
        state_dir=state,
        port=PORT,
        http_post=machine.post,
        http_get=machine.get,
        run=machine.run,
        start_service=machine.start_service,
        sleep=lambda seconds: None,
        clock=_Clock(),
        receipt_path=receipt_path,
        host=_host(machine, state),
    )


# ── the task XML ─────────────────────────────────────────────────────


PYTHONW = r"C:\Users\Ada Lovelace\.local\state\ciaobot\updates\1.2.3\tool\ciaobot\Scripts\pythonw.exe"
STAGE = r"C:\Users\Ada Lovelace\.local\state\ciaobot\updates\1.2.3"


def _parse(xml: str) -> ElementTree.Element:
    # Round-tripped through the bytes Task Scheduler reads: UTF-16 with a BOM.
    return ElementTree.fromstring(xml.encode("utf-16"))


def test_updater_task_has_no_trigger_and_a_thirty_minute_limit() -> None:
    xml = ws.render_oneshot_task_xml(
        python=PYTHONW,
        arguments=["-I", "-m", "ciao.engine_update", "run-apply", "--operation", "op-1"],
        workdir=STAGE,
        user=USER,
    )
    root = _parse(xml)

    assert root.find(f"{NS}Triggers") is None
    assert root.findtext(f"{NS}RegistrationInfo/{NS}URI") == ws.UPDATER_TASK_NAME
    assert root.findtext(f"{NS}Settings/{NS}ExecutionTimeLimit") == "PT30M"
    # A failed swap is settled by its own rollback; Task Scheduler must not
    # run it a second time.
    assert root.find(f"{NS}Settings/{NS}RestartOnFailure") is None
    assert root.findtext(f"{NS}Settings/{NS}MultipleInstancesPolicy") == "IgnoreNew"
    assert root.findtext(f"{NS}Principals/{NS}Principal/{NS}LogonType") == "InteractiveToken"
    assert root.findtext(f"{NS}Principals/{NS}Principal/{NS}RunLevel") == "LeastPrivilege"
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}Command") == PYTHONW
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments") == (
        "-I -m ciao.engine_update run-apply --operation op-1"
    )
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}WorkingDirectory") == STAGE


def test_recover_task_runs_at_logon_and_every_minute_with_no_end() -> None:
    xml = ws.render_recover_task_xml(
        python=PYTHONW,
        arguments=["-I", "-m", "ciao.engine_update", "run-recover", "--operation", "op-1"],
        workdir=STAGE,
        user=USER,
        start="2026-10-01T18:30:00",
    )
    root = _parse(xml)

    assert root.findtext(f"{NS}RegistrationInfo/{NS}URI") == ws.RECOVER_TASK_NAME
    assert root.findtext(f"{NS}Triggers/{NS}LogonTrigger/{NS}UserId") == USER
    repetition = root.find(f"{NS}Triggers/{NS}TimeTrigger/{NS}Repetition")
    assert repetition is not None
    assert repetition.findtext(f"{NS}Interval") == "PT1M"
    # No Duration: the repetition outlives reboots until the task is deleted.
    assert repetition.find(f"{NS}Duration") is None
    assert root.findtext(f"{NS}Triggers/{NS}TimeTrigger/{NS}StartBoundary") == "2026-10-01T18:30:00"
    assert root.findtext(f"{NS}Settings/{NS}ExecutionTimeLimit") == "PT30M"
    assert root.find(f"{NS}Settings/{NS}RestartOnFailure") is None


def test_recover_task_refuses_a_start_that_is_not_local_time() -> None:
    with pytest.raises(ValueError, match="start"):
        ws.render_recover_task_xml(
            python=PYTHONW, arguments=["-I"], workdir=STAGE, user=USER, start="2026-10-01T18:30:00Z"
        )


def test_update_task_arguments_are_quoted_and_escaped() -> None:
    xml = ws.render_oneshot_task_xml(
        python=PYTHONW, arguments=["-m", "a b", "<&>"], workdir=STAGE, user=USER
    )

    assert _parse(xml).findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments") == '-m "a b" <&>'


# ── task_command: what the registered engine task runs ──────────────


def _query(stdout: str, returncode: int = 0) -> Any:
    seen: list[tuple[tuple[str, ...], str]] = []

    def runner(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        seen.append((args, encoding))
        return subprocess.CompletedProcess(list(args), returncode, stdout, "")

    return runner, seen


def test_task_command_reads_the_registered_command_in_the_oem_code_page() -> None:
    body = (
        '<?xml version="1.0" encoding="UTF-16"?>\r\r\n'
        f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec>'
        f"<Command>{PYTHONW}</Command></Exec></Actions></Task>"
    )
    runner, seen = _query(body)

    assert ws.task_command(ws.TASK_NAME, runner=runner) == PYTHONW
    # schtasks prints /XML in the console's OEM code page, whatever the
    # declaration says.
    assert seen == [(("/Query", "/TN", ws.TASK_NAME, "/XML"), "oem")]


def test_task_command_is_none_for_a_path_the_code_page_could_not_print() -> None:
    body = (
        f'<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec>'
        f"<Command>C:\\Users\\?d?\\pythonw.exe</Command></Exec></Actions></Task>"
    )
    runner, _ = _query(body)

    assert ws.task_command(ws.TASK_NAME, runner=runner) is None


@pytest.mark.parametrize("stdout,returncode", [("", 1), ("not xml", 0), ("<Task/>", 0)])
def test_task_command_is_none_when_there_is_nothing_usable(stdout: str, returncode: int) -> None:
    runner, _ = _query(stdout, returncode)

    assert ws.task_command(ws.TASK_NAME, runner=runner) is None


# ── the host, one method at a time ───────────────────────────────────


def test_windows_update_host_is_an_update_host(tmp_path: Path) -> None:
    host: UpdateHost = WindowsUpdateHost(state_dir=tmp_path)

    assert isinstance(host, WindowsUpdateHost)


def test_engine_port_comes_from_the_task_workspace(tmp_path: Path) -> None:
    machine = _Machine(tmp_path)

    assert _host(machine, tmp_path / "updates").engine_port() == PORT


def test_spawn_updater_registers_then_runs_the_sibling_task(tmp_path: Path, user: None) -> None:
    op, state, _, machine = _staged(tmp_path)

    _host(machine, state).spawn_updater(op, op.env_python)

    definition = state / UPDATER_FILE_NAME
    assert machine.changing_calls() == [
        ["/Create", "/TN", ws.UPDATER_TASK_NAME, "/XML", str(definition), "/F"],
        ["/Run", "/TN", ws.UPDATER_TASK_NAME],
    ]
    root = ElementTree.fromstring(definition.read_bytes())
    assert definition.read_bytes()[:2] == b"\xff\xfe"
    # pythonw.exe beside the staged interpreter, so no console window opens.
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}Command") == ws.windowless_python(op.env_python)
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}Arguments") == (
        f"-I -m ciao.engine_update run-apply --operation {op.id}"
    )


def test_spawn_updater_failure_is_an_update_error(tmp_path: Path, user: None) -> None:
    op, state, _, machine = _staged(tmp_path)

    def refusing(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 1, "", "ERROR: Access is denied.")

    host = WindowsUpdateHost(schtasks=refusing, state_dir=state, task_dir=machine.task_dir)

    with pytest.raises(UpdateError, match="Access is denied"):
        host.spawn_updater(op, op.env_python)


def test_recovery_task_always_runs_from_the_staged_interpreter(tmp_path: Path, user: None) -> None:
    op, state, _, machine = _staged(tmp_path)
    previous = Path(op.stage_dir) / PREVIOUS_ENV_NAME / "Scripts" / "python.exe"

    # The transaction re-points the net at previous-env once the live env is
    # renamed; on Windows the staged env is never moved, and previous-env is
    # what the rollback renames back into place.
    written = _host(machine, state).install_recovery_agent(op, str(previous), state)

    assert written == state / RECOVER_FILE_NAME
    root = ElementTree.fromstring(written.read_bytes())
    assert root.findtext(f"{NS}Actions/{NS}Exec/{NS}Command") == ws.windowless_python(op.env_python)
    assert machine.changing_calls() == [
        ["/Create", "/TN", ws.RECOVER_TASK_NAME, "/XML", str(written), "/F"],
    ]


def test_stop_engine_ends_the_task_before_any_wait(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    machine.hold_lock()
    order: list[str] = []

    def wait() -> bool:
        order.append(f"wait after {machine.schtasks_calls[-1][0]}")
        return True

    assert _host(machine, state).stop_engine(wait=wait) is True
    assert machine.schtasks_calls == [["/End", "/TN", ws.TASK_NAME]]
    assert order == ["wait after /End"]


def test_stop_engine_is_false_while_the_engine_still_holds_its_lock(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    machine.hold_lock()

    def stubborn(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        # `/End` returns, but the process has not let go of its files.
        return subprocess.CompletedProcess(list(args), 0, "", "")

    host = WindowsUpdateHost(
        schtasks=stubborn, state_dir=state, task_dir=machine.task_dir,
        sleep=lambda seconds: None, clock=_Clock(),
    )

    try:
        # The port already closed; the lock is the only evidence left, and it says no.
        assert host.stop_engine(wait=lambda: True) is False
        # The rollback's call, with no port wait, is confirmed by the lock too.
        assert host.stop_engine() is False
    finally:
        machine.release_lock()


def test_stop_engine_does_not_wait_for_the_lock_when_the_port_never_closed(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    probed: list[str] = []
    host = _host(machine, state)
    host._engine_holds_lock = lambda: probed.append("lock") or False  # type: ignore[method-assign]

    assert host.stop_engine(wait=lambda: False) is False
    assert probed == []


def test_start_engine_runs_the_task_when_nothing_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import macos_service

    _, state, _, machine = _staged(tmp_path)
    monkeypatch.setattr(macos_service, "server_reachable", lambda port: False)

    assert _host(machine, state).start_engine().ok is True
    assert machine.schtasks_calls == [["/Run", "/TN", ws.TASK_NAME]]


def test_start_engine_does_not_run_an_engine_that_already_answers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import macos_service

    _, state, _, machine = _staged(tmp_path)
    monkeypatch.setattr(macos_service, "server_reachable", lambda port: port == PORT)

    assert _host(machine, state).start_engine().ok is True
    assert machine.schtasks_calls == []


def test_start_engine_reports_a_refused_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import macos_service

    _, _, _, machine = _staged(tmp_path)
    monkeypatch.setattr(macos_service, "server_reachable", lambda port: False)

    def refusing(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 1, "", "ERROR: The task is disabled.")

    result = WindowsUpdateHost(schtasks=refusing, task_dir=machine.task_dir).start_engine()

    assert result.ok is False
    assert "disabled" in result.message


def test_updater_running_is_the_update_lock(tmp_path: Path) -> None:
    from ciao.engine_update import acquire_lock, release_lock

    state = tmp_path / "updates"
    host = WindowsUpdateHost(state_dir=state)

    assert host.updater_running() is False
    handle = acquire_lock(state)
    try:
        assert host.updater_running() is True
    finally:
        release_lock(handle)
    assert host.swap_in_flight() is False


def test_move_env_refuses_while_the_engine_holds_its_lock(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    machine.hold_lock()
    try:
        with pytest.raises(UpdateError, match="still running"):
            _host(machine, state).move_env(machine.live_env, tmp_path / "elsewhere")
    finally:
        machine.release_lock()
    assert machine.live_env.is_dir()


def test_move_env_puts_what_is_in_the_way_aside_first(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    source = tmp_path / "source"
    _write_env(source, "9.9.9")
    host = _host(machine, state)

    host.move_env(source, machine.live_env)

    assert _env_version(machine.live_env) == "9.9.9"
    assert not source.exists()
    # What was there was renamed aside and then deleted.
    assert list((state / DISCARDED_DIR_NAME).iterdir()) == []


def test_move_env_does_not_depend_on_the_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import windows_update

    _, state, _, machine = _staged(tmp_path)
    source = tmp_path / "source"
    _write_env(source, "9.9.9")
    # Another process maps a file in the env in the way, so it cannot be deleted.
    monkeypatch.setattr(windows_update, "_rmtree_best_effort", lambda path: False)

    _host(machine, state).move_env(source, machine.live_env)

    assert _env_version(machine.live_env) == "9.9.9"
    [aside] = list((state / DISCARDED_DIR_NAME).iterdir())
    assert _env_version(aside) == FROM_VERSION


def test_env_layout_is_scripts_python_exe(tmp_path: Path) -> None:
    host = WindowsUpdateHost(state_dir=tmp_path)
    _write_env(tmp_path / "tool" / "ciaobot", TO_VERSION)

    assert host.env_python(tmp_path / "env") == tmp_path / "env" / "Scripts" / "python.exe"
    assert host.find_staged_python(tmp_path / "tool") == (
        tmp_path / "tool" / "ciaobot" / "Scripts" / "python.exe"
    )


def test_find_staged_python_refuses_anything_but_one_env(tmp_path: Path) -> None:
    host = WindowsUpdateHost(state_dir=tmp_path)
    (tmp_path / "tool").mkdir()

    with pytest.raises(UpdateError, match="found 0"):
        host.find_staged_python(tmp_path / "tool")


def test_pins_drop_the_package_being_installed() -> None:
    assert _pins(FREEZE) == "anyio==4.4.0\nhttpx==0.27.0\n"
    assert _pins("Ciaobot==1.2.3\n# comment\n\nidna==3.7\n") == "idna==3.7\n"


def test_install_env_is_an_offline_copy_mode_reinstall_with_relative_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import install_receipt

    op, state, receipt_path, machine = _staged(tmp_path)
    monkeypatch.setattr(install_receipt, "default_receipt_path", lambda: receipt_path)
    (tmp_path / "uv.exe").write_text("uv", encoding="utf-8")
    monkeypatch.setattr("ciao.engine_update.os.access", lambda path, mode: True)
    host = _host(machine, state)
    machine.live_env.rename(Path(op.stage_dir) / PREVIOUS_ENV_NAME)

    host.install_env(
        op, Path(op.env_python).parent.parent, machine.live_env, bin_dir=machine.bin_dir, wheel=Path(op.wheel)
    )

    [(argv, kwargs)] = machine.uv_calls
    assert argv == [
        str(tmp_path / "uv.exe"),
        "tool", "install", "--offline", "--force", "--link-mode", "copy",
        "--python", f"{sys.version_info.major}.{sys.version_info.minor}",
        # Relative to the stage dir: uv splits an absolute path with a space.
        "--constraint", CONSTRAINTS_NAME,
        Path(op.wheel).name,
    ]
    assert kwargs["cwd"] == op.stage_dir
    assert kwargs["env"]["UV_TOOL_DIR"] == str(machine.tools)
    assert kwargs["env"]["UV_TOOL_BIN_DIR"] == str(machine.bin_dir)
    assert (Path(op.stage_dir) / CONSTRAINTS_NAME).read_text(encoding="utf-8") == _pins(FREEZE)
    assert _env_version(machine.live_env) == TO_VERSION
    # The staged env is never moved: it is the updater's own interpreter.
    assert Path(op.env_python).is_file()


def _install(op: Operation, machine: _Machine, state: Path, receipt_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from ciao import install_receipt

    monkeypatch.setattr(install_receipt, "default_receipt_path", lambda: receipt_path)
    (receipt_path.parent / "uv.exe").write_text("uv", encoding="utf-8")
    monkeypatch.setattr("ciao.engine_update.os.access", lambda path, mode: True)
    _host(machine, state).install_env(
        op, Path(op.env_python).parent.parent, machine.live_env, bin_dir=machine.bin_dir, wheel=Path(op.wheel)
    )


def test_install_env_refuses_a_record_with_no_pins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    op.env_freeze = ""

    with pytest.raises(UpdateError, match="no resolved pins"):
        _install(op, machine, state, receipt_path, monkeypatch)
    assert machine.uv_calls == []


def test_install_env_names_a_purged_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    machine.uv_error = "error: Network connectivity is disabled, but `anyio==4.4.0` was not found in the cache"

    with pytest.raises(UpdateError, match="offline reinstall failed.*not found in the cache"):
        _install(op, machine, state, receipt_path, monkeypatch)


def test_install_env_refuses_a_reinstall_without_its_launchers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    original = machine.run

    def without_bin(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        result = original(argv, **kwargs)
        (machine.bin_dir / "ciaobot.exe").unlink()
        return result

    machine.run = without_bin  # type: ignore[method-assign]

    with pytest.raises(UpdateError, match=r"no ciaobot\.exe"):
        _install(op, machine, state, receipt_path, monkeypatch)


def test_after_env_restored_puts_the_old_launchers_back(tmp_path: Path) -> None:
    _, state, _, machine = _staged(tmp_path)
    wheel = _wheel(tmp_path / "new.whl", TO_VERSION)
    for name in ENTRY_POINTS:
        (machine.bin_dir / f"{name}.exe").write_text("new launcher", encoding="utf-8")
    (machine.live_env / "Scripts" / "ciaobot.exe").unlink()

    _host(machine, state).after_env_restored(machine.live_env, bin_dir=machine.bin_dir, wheel=wheel)

    assert (machine.bin_dir / "ciao.exe").read_bytes() == (
        machine.live_env / "Scripts" / "ciao.exe"
    ).read_bytes()
    # A launcher the old env does not have would name the discarded new env.
    assert not (machine.bin_dir / "ciaobot.exe").exists()


def test_uv_candidates_and_stdio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    host = WindowsUpdateHost(state_dir=tmp_path)

    assert host.uv_candidates() == [str(home / ".local" / "bin" / "uv.exe")]
    # A console process keeps its own stdio; nothing is opened.
    host.redirect_detached_stdio(tmp_path / "state")
    assert not (tmp_path / "state").exists()


def test_redirect_detached_stdio_opens_the_log_under_pythonw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import logging

    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    root_logger = logging.getLogger()
    handlers = list(root_logger.handlers)

    WindowsUpdateHost(state_dir=tmp_path).redirect_detached_stdio(tmp_path / "state")
    try:
        print("hello from the updater")
        assert "hello from the updater" in (tmp_path / "state" / "updater.log").read_text(encoding="utf-8")
    finally:
        log = sys.stderr
        for handler in root_logger.handlers[:]:
            if handler not in handlers:
                root_logger.removeHandler(handler)
        if log is not None:
            log.close()


@pytest.mark.skipif(sys.platform != "win32", reason="SIGBREAK exists only on Windows")
def test_interrupt_signals_are_term_and_break() -> None:
    import signal

    assert WindowsUpdateHost().interrupt_signals() == (signal.SIGTERM, signal.SIGBREAK)


# ── the shared transaction, driven through the Windows host ─────────


def test_run_apply_rebuilds_the_live_env_and_keeps_the_old_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    monkeypatch.setattr("ciao.install_receipt.default_receipt_path", lambda: receipt_path)
    (tmp_path / "uv.exe").write_text("uv", encoding="utf-8")
    monkeypatch.setattr("ciao.engine_update.os.access", lambda path, mode: True)

    result = _run(op, state, receipt_path, machine)

    assert result.phase == "applied", result.error
    assert _env_version(machine.live_env) == TO_VERSION
    assert _env_version(Path(op.stage_dir) / PREVIOUS_ENV_NAME) == FROM_VERSION
    assert read_receipt(receipt_path).version == TO_VERSION  # type: ignore[union-attr]
    assert machine.changing_calls() == [
        # The engine is out before a single file moves.
        ["/End", "/TN", ws.TASK_NAME],
        # The net is re-registered (from the staged interpreter) after the rename.
        ["/Create", "/TN", ws.RECOVER_TASK_NAME, "/XML", str(state / RECOVER_FILE_NAME), "/F"],
        ["/Run", "/TN", ws.TASK_NAME],
        # Nothing is left to delete, so the net is retired.
        ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"],
    ]
    assert len(machine.uv_calls) == 1


def test_run_apply_rolls_back_when_the_cache_was_purged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    monkeypatch.setattr("ciao.install_receipt.default_receipt_path", lambda: receipt_path)
    (tmp_path / "uv.exe").write_text("uv", encoding="utf-8")
    monkeypatch.setattr("ciao.engine_update.os.access", lambda path, mode: True)
    launchers = {name: (machine.bin_dir / f"{name}.exe").read_bytes() for name in ENTRY_POINTS}
    machine.uv_error = "error: Network connectivity is disabled, but `anyio==4.4.0` was not found in the cache"

    result = _run(op, state, receipt_path, machine)

    assert result.phase == "rolled_back", result.error
    assert "not found in the cache" in result.error
    assert f"rolled back to {FROM_VERSION}" in result.error
    assert _env_version(machine.live_env) == FROM_VERSION
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()
    assert {name: (machine.bin_dir / f"{name}.exe").read_bytes() for name in ENTRY_POINTS} == launchers
    assert read_receipt(receipt_path).version == FROM_VERSION  # type: ignore[union-attr]
    # The old engine was started again, last.
    assert machine.changing_calls()[-2:] == [
        ["/Run", "/TN", ws.TASK_NAME],
        ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"],
    ]
    assert machine.up is True


def test_run_apply_touches_nothing_when_the_engine_will_not_let_go(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    machine.hold_lock()
    original = machine.schtasks

    def end_without_release(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
        if list(args[:1]) == ["/End"]:
            machine.schtasks_calls.append(list(args))
            machine.up = False
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return original(*args, encoding=encoding)

    machine.schtasks = end_without_release  # type: ignore[method-assign]
    try:
        result = _run(op, state, receipt_path, machine)
    finally:
        machine.release_lock()

    assert result.phase == "failed"
    assert "nothing was touched" in result.error
    assert _env_version(machine.live_env) == FROM_VERSION
    assert machine.uv_calls == []
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()


def test_run_apply_refuses_a_task_running_another_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    machine.task_program = r"C:\elsewhere\Scripts\pythonw.exe"

    result = _run(op, state, receipt_path, machine)

    assert result.phase == "failed"
    assert "elsewhere" in result.error
    assert machine.changing_calls() == []


def test_recover_apply_restores_an_interrupted_swap_and_retires_the_net(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path, phase="swapping")
    # Interrupted after the rename and half-way through the reinstall.
    previous = Path(op.stage_dir) / PREVIOUS_ENV_NAME
    machine.live_env.rename(previous)
    (machine.live_env / "Lib").mkdir(parents=True)
    machine.up = False

    result = recover_apply(
        op.id,
        state_dir=state,
        port=PORT,
        http_post=lambda url: pytest.fail("recovery must post nothing"),
        http_get=machine.get,
        start_service=machine.start_service,
        sleep=lambda seconds: None,
        clock=_Clock(),
        receipt_path=receipt_path,
        host=_host(machine, state),
    )

    assert result is not None and result.phase == "rolled_back", result and result.error
    assert _env_version(machine.live_env) == FROM_VERSION
    assert machine.changing_calls()[-1] == ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"]
    assert read_operation(state).phase == "rolled_back"  # type: ignore[union-attr]


def test_retire_keeps_the_net_while_a_delete_is_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao import windows_update

    op, state, _, machine = _staged(tmp_path, phase="applied")
    stuck = state / DISCARDED_DIR_NAME / "20261001T100000-ciaobot"
    _write_env(stuck, FROM_VERSION)
    (state / RECOVER_FILE_NAME).write_text("definition", encoding="utf-8")
    monkeypatch.setattr(windows_update, "_rmtree_best_effort", lambda path: False)
    host = _host(machine, state)

    host.retire_recovery_agent(state)

    # The next once-a-minute tick retries the delete, so the task stays.
    assert machine.changing_calls() == []
    assert (state / RECOVER_FILE_NAME).exists()

    monkeypatch.undo()
    host.retire_recovery_agent(state)

    assert not stuck.exists()
    assert not (state / RECOVER_FILE_NAME).exists()
    assert machine.changing_calls() == [["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"]]


def test_retire_also_retries_a_superseded_previous_env(tmp_path: Path) -> None:
    op, state, _, machine = _staged(tmp_path, phase="applied")
    older = state / "1.2.1" / PREVIOUS_ENV_NAME
    _write_env(older, "1.2.0")
    own = Path(op.stage_dir) / PREVIOUS_ENV_NAME
    _write_env(own, FROM_VERSION)

    _host(machine, state).retire_recovery_agent(state)

    assert not older.exists()
    # This update's own previous-env is its one rollback generation.
    assert own.exists()


# ── review round 1 (#900) ────────────────────────────────────────────


def _recover(op: Operation, state: Path, receipt_path: Path, machine: _Machine, operation_id: str | None = None) -> Any:
    return recover_apply(
        operation_id or op.id,
        state_dir=state,
        port=PORT,
        http_post=lambda url: pytest.fail("recovery must post nothing"),
        http_get=machine.get,
        start_service=machine.start_service,
        sleep=lambda seconds: None,
        clock=_Clock(),
        receipt_path=receipt_path,
        host=_host(machine, state),
    )


def _uv_ready(tmp_path: Path, receipt_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ciao.install_receipt.default_receipt_path", lambda: receipt_path)
    (tmp_path / "uv.exe").write_text("uv", encoding="utf-8")
    monkeypatch.setattr("ciao.engine_update.os.access", lambda path, mode: True)


def test_recover_starts_the_engine_after_a_helper_died_while_stopping(tmp_path: Path, user: None) -> None:
    op, state, receipt_path, machine = _staged(tmp_path, phase="stopping")
    # The helper had sent /End and died: the engine is down, nothing moved.
    machine.up = False

    result = _recover(op, state, receipt_path, machine)

    assert result is not None and result.phase == "failed"
    assert "nothing was touched" in result.error
    assert machine.up is True
    assert machine.changing_calls() == [
        ["/Run", "/TN", ws.TASK_NAME],
        ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"],
    ]
    assert _env_version(machine.live_env) == FROM_VERSION
    assert not (Path(op.stage_dir) / PREVIOUS_ENV_NAME).exists()


def _flaky_restore(monkeypatch: pytest.MonkeyPatch, failures: int) -> list[str]:
    """Make renaming previous-env back fail ``failures`` times (a scanner's handle)."""
    import os as os_module

    real = os_module.replace
    seen: list[str] = []

    def replace(src: Any, dst: Any) -> None:
        if Path(src).name == PREVIOUS_ENV_NAME and len(seen) < failures:
            seen.append(str(src))
            raise PermissionError(13, "The process cannot access the file", str(src))
        real(src, dst)

    monkeypatch.setattr("ciao.windows_update.os.replace", replace)
    return seen


def test_rollback_retries_a_restore_rename_that_fails_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    _uv_ready(tmp_path, receipt_path, monkeypatch)
    machine.uv_error = "error: not found in the cache"
    refused = _flaky_restore(monkeypatch, failures=2)

    result = _run(op, state, receipt_path, machine)

    assert len(refused) == 2
    assert result.phase == "rolled_back", result.error
    assert _env_version(machine.live_env) == FROM_VERSION
    assert machine.changing_calls()[-1] == ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"]


def test_a_restore_that_never_lands_keeps_the_net_until_a_tick_restores_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    _uv_ready(tmp_path, receipt_path, monkeypatch)
    machine.uv_error = "error: not found in the cache"
    _flaky_restore(monkeypatch, failures=10_000)

    result = _run(op, state, receipt_path, machine)

    assert result.phase == "rollback_failed"
    previous = Path(op.stage_dir) / PREVIOUS_ENV_NAME
    assert _env_version(previous) == FROM_VERSION
    # The intact old install is still on disk, so the net is not retired.
    assert ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"] not in machine.changing_calls()

    # The scanner lets go; the next recovery tick restores it and retires.
    monkeypatch.undo()
    _uv_ready(tmp_path, receipt_path, monkeypatch)
    ticked = _recover(op, state, receipt_path, machine)

    assert ticked is not None and ticked.phase == "rolled_back", ticked and ticked.error
    assert _env_version(machine.live_env) == FROM_VERSION
    assert not previous.exists()
    assert machine.changing_calls()[-1] == ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"]


def test_rollback_touches_no_file_when_the_stop_is_not_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, user: None
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    _uv_ready(tmp_path, receipt_path, monkeypatch)
    original_run = machine.run

    def uv_fails_and_something_starts_the_engine(argv: list[str], **kwargs: Any) -> Any:
        if "-c" in argv:
            return original_run(argv, **kwargs)
        # Half an install, and an engine that grabs its lock and will not let go.
        (machine.live_env / "Lib").mkdir(parents=True, exist_ok=True)
        machine.hold_lock()
        raise subprocess.CalledProcessError(2, argv, output="", stderr="error: interrupted")

    machine.run = uv_fails_and_something_starts_the_engine  # type: ignore[method-assign]
    original = machine.schtasks

    def end_keeps_the_lock(*args: str, encoding: str = "utf-8") -> Any:
        if list(args[:1]) == ["/End"] and machine.held_lock is not None:
            machine.schtasks_calls.append(list(args))
            return subprocess.CompletedProcess(list(args), 0, "", "")
        return original(*args, encoding=encoding)

    machine.schtasks = end_keeps_the_lock  # type: ignore[method-assign]
    try:
        result = _run(op, state, receipt_path, machine)
    finally:
        machine.release_lock()

    assert result.phase == "rollback_failed"
    assert "did not stop" in result.error
    # Nothing deleted or renamed: the half install and the old env are both still there.
    assert (machine.live_env / "Lib").is_dir()
    assert _env_version(Path(op.stage_dir) / PREVIOUS_ENV_NAME) == FROM_VERSION
    assert ["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"] not in machine.changing_calls()


def test_a_stale_net_retires_once_the_newer_record_is_settled(tmp_path: Path, user: None) -> None:
    op, state, receipt_path, machine = _staged(tmp_path, phase="rolled_back")
    newer = Operation(
        id="20261002T100000-1.2.4", phase="staged", from_version=FROM_VERSION, to_version="1.2.4",
        started_at="", updated_at="", stage_dir=str(state / "1.2.4"),
    )
    write_operation(newer, state)

    assert _recover(op, state, receipt_path, machine) is None
    assert machine.changing_calls() == [["/Delete", "/TN", ws.RECOVER_TASK_NAME, "/F"]]
    # The record is not this net's to touch.
    assert read_operation(state) == newer


def test_a_stale_net_stays_while_the_newer_update_is_in_flight(tmp_path: Path, user: None) -> None:
    op, state, receipt_path, machine = _staged(tmp_path, phase="rolled_back")
    newer = Operation(
        id="20261002T100000-1.2.4", phase="applying", from_version=FROM_VERSION, to_version="1.2.4",
        started_at="", updated_at="", stage_dir=str(state / "1.2.4"),
    )
    write_operation(newer, state)

    assert _recover(op, state, receipt_path, machine) is None
    assert machine.changing_calls() == []


def test_task_command_is_none_for_a_command_that_is_not_ascii() -> None:
    # Best fit can print `Łukasz` as `Lukasz` with no `?`; any non-ASCII
    # character means the code page was involved, so nothing is trusted.
    body = (
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Actions><Exec>'
        "<Command>C:\\Users\\\u00c9mile\\pythonw.exe</Command></Exec></Actions></Task>"
    )
    runner, _ = _query(body)

    assert ws.task_command(ws.TASK_NAME, runner=runner) is None


def test_install_env_rechecks_the_wheel_digest_before_uv_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op, state, receipt_path, machine = _staged(tmp_path)
    with Path(op.wheel).open("ab") as handle:
        handle.write(b"swapped after the pre-flight")

    with pytest.raises(UpdateError, match="no longer matches the digest"):
        _install(op, machine, state, receipt_path, monkeypatch)
    assert machine.uv_calls == []


def test_staging_a_version_whose_update_is_pending_is_refused(tmp_path: Path) -> None:
    from ciao.engine_update import stage_update

    op, state, _, _ = _staged(tmp_path, phase="swapping")

    with pytest.raises(UpdateError, match="still swapping"):
        stage_update(
            TO_VERSION,
            current_version=FROM_VERSION,
            state_dir=state,
            fetch=lambda *args, **kwargs: pytest.fail("nothing may be fetched"),
        )
    # The interpreter the recovery task runs from is still there.
    assert Path(op.env_python).is_file()
    assert read_operation(state).phase == "swapping"  # type: ignore[union-attr]
