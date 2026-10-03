"""ciao.windows_service: the task XML is exact, and every schtasks call is bounded.

The renderer is pure and is checked against a golden document, so the trigger,
the principal and the `pythonw.exe` action cannot drift unnoticed. The helpers
are checked through a recording `subprocess.run`, because what matters about a
`schtasks` call is its argv and its bounds (timeout, explicit encoding, no
shell) rather than what Windows does with it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from ciao import windows_service as ws

PYTHONW = r"C:\Users\Ada Lovelace\AppData\Roaming\uv\tools\ciaobot\Scripts\pythonw.exe"
WORKSPACE = r"C:\Users\Ada Lovelace\ciao"
USER = r"DESKTOP-1\ada"

# Raw, on one line with the opening quotes: the document is full of `\Users` and
# `\Ciaobot`, which a normal string reads as a `\U` unicode escape, and a raw
# string does not honour a trailing backslash line continuation.
GOLDEN = r"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>Ciaobot engine: starts `ciao supervise` when this user logs on.</Description>
    <URI>\Ciaobot\Engine</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>DESKTOP-1\ada</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>DESKTOP-1\ada</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>C:\Users\Ada Lovelace\AppData\Roaming\uv\tools\ciaobot\Scripts\pythonw.exe</Command>
      <Arguments>-m ciao.cli supervise</Arguments>
      <WorkingDirectory>C:\Users\Ada Lovelace\ciao</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


class _Schtasks:
    """A recording stand-in for ``subprocess.run`` returning scripted results."""

    def __init__(self, *results: subprocess.CompletedProcess[str]) -> None:
        self.calls: list[tuple[list[str], dict[str, object]]] = []
        self.results = list(results)

    def __call__(self, command, *args, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append((list(command), kwargs))
        return self.results.pop(0) if self.results else subprocess.CompletedProcess(command, 0, "", "")

    @property
    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


def _ok(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def test_render_task_xml_matches_golden() -> None:
    assert ws.render_task_xml(python=PYTHONW, workspace=WORKSPACE, user=USER) == GOLDEN


def test_rendered_xml_is_well_formed_utf16() -> None:
    from xml.etree import ElementTree

    ElementTree.fromstring(GOLDEN.encode("utf-16"))   # declaration says UTF-16


def test_render_escapes_xml_metacharacters() -> None:
    xml = ws.render_task_xml(python=PYTHONW, workspace=r"C:\a&b <x>", user=USER)

    assert r"C:\a&amp;b &lt;x&gt;" in xml


# A task whose command or working directory Task Scheduler cannot use fails at
# logon, silently; the renderer refuses the values it cannot vouch for instead.
# PureWindowsPath reads `//server/share` as a UNC drive, so the forward-slash
# spelling has to be rejected as firmly as the backslash one.
@pytest.mark.parametrize(
    "kwargs",
    [
        {"python": "pythonw.exe"},
        {"python": r"\\server\share\pythonw.exe"},
        {"python": "//server/share/pythonw.exe"},
        {"workspace": "ciao"},
        {"workspace": "C:\\w\nx"},
        {"workspace": "//server/share/ciao"},
        {"user": ""},
    ],
)
def test_render_rejects_bad_values(kwargs: dict[str, str]) -> None:
    args = {"python": PYTHONW, "workspace": WORKSPACE, "user": USER, **kwargs}

    with pytest.raises(ValueError):
        ws.render_task_xml(**args)  # type: ignore[arg-type]


def test_windowless_python() -> None:
    assert ws.windowless_python(r"C:\t\Scripts\python.exe") == r"C:\t\Scripts\pythonw.exe"
    assert ws.windowless_python(r"C:\t\Scripts\pythonw.exe") == r"C:\t\Scripts\pythonw.exe"

    with pytest.raises(ValueError):
        ws.windowless_python(r"C:\t\ciao.exe")


def test_register_task_runs_schtasks_create_with_bounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _Schtasks()
    monkeypatch.setattr(ws.subprocess, "run", fake)

    ws.register_task(Path(r"C:\x\t.xml"))

    assert fake.argvs == [
        ["schtasks.exe", "/Create", "/TN", r"\Ciaobot\Engine", "/XML", r"C:\x\t.xml", "/F"]
    ]
    kwargs = fake.calls[0][1]
    assert kwargs["timeout"] == ws.SCHTASKS_TIMEOUT_S
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    assert kwargs["check"] is False
    # A shell would let a workspace path with a metacharacter in it run a command.
    assert "shell" not in kwargs


@pytest.mark.parametrize(
    ("call", "verb"),
    [(ws.start_task, "/Run"), (ws.stop_task, "/End")],
)
def test_start_and_stop_run_one_verb(
    call: object, verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _Schtasks()
    monkeypatch.setattr(ws.subprocess, "run", fake)

    call()  # type: ignore[operator]

    assert fake.argvs == [["schtasks.exe", verb, "/TN", r"\Ciaobot\Engine"]]


def test_unregister_task_probes_then_deletes(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _Schtasks()
    monkeypatch.setattr(ws.subprocess, "run", fake)

    assert ws.unregister_task() is True

    assert fake.argvs == [
        ["schtasks.exe", "/Query", "/TN", r"\Ciaobot\Engine"],
        ["schtasks.exe", "/Delete", "/TN", r"\Ciaobot\Engine", "/F"],
    ]


def test_unregister_task_is_a_noop_when_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    absent = subprocess.CompletedProcess(["schtasks.exe"], 1, "", "ERROR: not found")
    fake = _Schtasks(absent)
    monkeypatch.setattr(ws.subprocess, "run", fake)

    assert ws.unregister_task() is False

    assert [argv[1] for argv in fake.argvs] == ["/Query"]


@pytest.mark.parametrize("returncode", [0, 1])
def test_task_exists_reads_the_query_exit_code(
    returncode: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = subprocess.CompletedProcess(["schtasks.exe"], returncode, "", "")
    fake = _Schtasks(completed)
    monkeypatch.setattr(ws.subprocess, "run", fake)

    assert ws.task_exists() is (returncode == 0)
    assert fake.argvs == [["schtasks.exe", "/Query", "/TN", r"\Ciaobot\Engine"]]


def test_schtasks_failure_carries_the_tool_message(monkeypatch: pytest.MonkeyPatch) -> None:
    failed = subprocess.CompletedProcess(
        ["schtasks.exe"], 1, "", "ERROR: Access is denied."
    )
    monkeypatch.setattr(ws.subprocess, "run", _Schtasks(failed))

    with pytest.raises(ws.WindowsServiceError, match="Access is denied."):
        ws.register_task(Path(r"C:\x\t.xml"))


def test_schtasks_timeout_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def timeout(*args: object, **kwargs: object):
        raise subprocess.TimeoutExpired(["schtasks.exe"], ws.SCHTASKS_TIMEOUT_S)

    monkeypatch.setattr(ws.subprocess, "run", timeout)

    with pytest.raises(ws.WindowsServiceError, match="timed out"):
        ws.task_exists()


def test_missing_schtasks_executable_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(*args: object, **kwargs: object):
        raise FileNotFoundError("schtasks.exe")

    monkeypatch.setattr(ws.subprocess, "run", missing)

    with pytest.raises(ws.WindowsServiceError, match="schtasks.exe"):
        ws.task_exists()


def test_write_task_definition_writes_utf16_with_bom(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `windowless_python` is PureWindowsPath-based, so a POSIX tmp path never
    # reaches the real one; `render_task_xml` is stubbed for the same reason.
    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(ws, "windowless_python", lambda _python: str(pythonw))
    monkeypatch.setattr(ws, "render_task_xml", lambda **kwargs: "<Task/>")
    monkeypatch.setattr(ws, "current_user", lambda: USER)

    written = ws.write_task_definition(
        workspace=Path(r"C:\w"), python=str(pythonw), directory=tmp_path / "defs"
    )

    assert written == tmp_path / "defs" / ws.TASK_FILE_NAME
    raw = written.read_bytes()
    assert raw.startswith(b"\xff\xfe")
    assert raw.decode("utf-16") == "<Task/>"


def test_write_task_definition_needs_the_windowless_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pythonw = tmp_path / "pythonw.exe"
    monkeypatch.setattr(ws, "windowless_python", lambda _python: str(pythonw))
    monkeypatch.setattr(ws, "current_user", lambda: USER)

    with pytest.raises(ws.WindowsServiceError, match="does not exist"):
        ws.write_task_definition(workspace=Path(r"C:\w"), directory=tmp_path)


def test_default_task_dir_honours_the_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path))

    assert ws.default_task_dir() == tmp_path


def test_task_dir_follows_localappdata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("CIAO_LAUNCH_AGENTS_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))

    expected = tmp_path / "Local" / "Ciaobot" / "service"

    assert ws.default_task_dir() == expected
    # The live dir is the operator's real install, so the override cannot reach it.
    assert ws.live_task_dir() == expected
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path))
    assert ws.live_task_dir() == expected


def test_current_user_uses_domain_and_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USERDOMAIN", "D")
    monkeypatch.setenv("USERNAME", "u")

    assert ws.current_user() == "D\\u"

    monkeypatch.delenv("USERDOMAIN", raising=False)
    assert ws.current_user() == "u"


def test_current_user_without_a_username_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("USERNAME", raising=False)

    with pytest.raises(ws.WindowsServiceError, match="USERNAME"):
        ws.current_user()


def test_spawn_delayed_start_is_a_detached_windowless_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def fake_popen(command, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls.append((list(command), kwargs))
        return subprocess.Popen

    monkeypatch.setattr(ws.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(ws, "windowless_python", lambda python: python)

    assert ws.spawn_delayed_start() is True

    argv, kwargs = calls[0]
    assert argv[0] == sys.executable
    assert argv[1] == "-c"
    script = argv[2]
    assert "schtasks.exe" in script
    assert "/Run" in script
    assert "Ciaobot" in script
    assert f"time.sleep({ws.HANDOFF_DELAY_S})" in script
    flags = int(kwargs["creationflags"])  # type: ignore[call-overload]
    assert flags & ws._CREATE_NO_WINDOW
    assert flags & ws._DETACHED_PROCESS
    assert flags & ws._CREATE_NEW_PROCESS_GROUP
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_spawn_delayed_start_reports_a_failed_spawn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail(*args: object, **kwargs: object):
        raise OSError("cannot spawn")

    monkeypatch.setattr(ws.subprocess, "Popen", fail)
    monkeypatch.setattr(ws, "windowless_python", lambda python: python)

    assert ws.spawn_delayed_start() is False


def test_spawn_delayed_start_reports_an_interpreter_it_cannot_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """schedule_server_handoff is documented to return a bool, never to raise."""

    def fail(*args: object, **kwargs: object):
        raise AssertionError("Popen must not run without a windowless interpreter")

    def refuse(python: str) -> str:
        raise ValueError(f"Expected python.exe or pythonw.exe, got {python!r}")

    monkeypatch.setattr(ws.subprocess, "Popen", fail)
    monkeypatch.setattr(ws, "windowless_python", refuse)

    assert ws.spawn_delayed_start() is False


@pytest.mark.skipif(sys.platform != "win32", reason="needs schtasks.exe")
def test_register_query_delete_roundtrip_with_real_schtasks(tmp_path: Path) -> None:
    name = f"\\Ciaobot\\Test-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    pythonw = ws.windowless_python(sys.executable)
    xml = ws.render_task_xml(python=pythonw, workspace=str(tmp_path), user=ws.current_user())
    xml = xml.replace(ws.TASK_NAME, name)          # unique URI
    definition = tmp_path / "task.xml"
    definition.write_bytes(xml.encode("utf-16"))
    try:
        ws.register_task(definition, name)
        assert ws.task_exists(name)
    finally:
        ws.unregister_task(name)
    assert not ws.task_exists(name)


@pytest.mark.skipif(sys.platform != "win32", reason="a task definition names a Windows WorkingDirectory")
def test_task_workspace_is_the_discovery_identity(tmp_path: Path) -> None:
    """The WorkingDirectory is exactly what bare-shell discovery reads.

    `ciao.install_discovery._discover_windows` calls `task_workspace` on the
    live definition to answer "which install is this shell talking to?", so the
    reader that identity rests on is pinned here: a definition written by the
    renderer yields its workspace, and a definition that is not there yields
    None rather than an error (no install to disagree with).
    """
    definition = tmp_path / ws.TASK_FILE_NAME
    definition.write_bytes(
        ws.render_task_xml(python=PYTHONW, workspace=WORKSPACE, user=USER).encode("utf-16")
    )

    assert ws.task_workspace(definition) == Path(WORKSPACE)
    assert ws.task_workspace(tmp_path / "absent.xml") is None