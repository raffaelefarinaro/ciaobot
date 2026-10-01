"""`ciao service` and `ciao setup --load-launchd` on Windows (#845).

`sys.platform` is `win32` and every `schtasks` verb is replaced, so the CLI
wiring is checked without a Windows box. What is asserted is the shape of the
wiring, not the tool's behaviour (which `tests/test_windows_service.py` checks
against recorded argv): which definition each command writes, which workspace
reaches that definition, and which verbs are issued in which order.

The renderer is stubbed throughout. `render_task_xml` refuses a POSIX working
directory -- Task Scheduler needs a drive letter -- so the stub writes the same
document around whatever workspace it was handed; which one it is handed is the
part this issue is about. `tests/test_windows_service.py` pins the real template
against a golden document.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from ciao import cli
from ciao import macos_service
from ciao import service_backend
from ciao import windows_service as ws

USER = r"DESKTOP-1\ada"
TASK_NS = ws.TASK_NS.strip("{}")

# `login` takes a positional and `migrate` a required path; the rest take none.
UNAVAILABLE_ARGV = {
    "login": ["service", "login", "enable", "--json"],
    "update-engine": ["service", "update-engine", "--json"],
    "migrate": ["service", "migrate", "--app-bundle", r"C:\apps\Ciaobot.app", "--json"],
    "migration-classify": ["service", "migration-classify", "--json"],
    "rollback": ["service", "rollback", "--json"],
}


class _Schtasks:
    """A recording stand-in for the ``schtasks`` verbs the CLI reaches for."""

    def __init__(self, *, registered: bool = False) -> None:
        self.registered = registered
        self.calls: list[tuple[str, ...]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> "_Schtasks":
        for name in ("task_exists", "register_task", "start_task", "stop_task"):
            monkeypatch.setattr(ws, name, getattr(self, name))
        return self

    @property
    def verbs(self) -> list[str]:
        return [call[0] for call in self.calls]

    def task_exists(self, name: str = ws.TASK_NAME) -> bool:
        self.calls.append(("/Query", name))
        return self.registered

    def register_task(self, definition: Path, name: str = ws.TASK_NAME) -> None:
        self.calls.append(("/Create", name, str(definition)))
        self.registered = True

    def start_task(self, name: str = ws.TASK_NAME) -> None:
        self.calls.append(("/Run", name))

    def stop_task(self, name: str = ws.TASK_NAME) -> None:
        self.calls.append(("/End", name))


class _PlatformShim:
    """`cli.sys` with another `platform`, leaving the real module alone.

    `monkeypatch.setattr(sys, "platform", "win32")` patches the one `sys` the
    whole process shares, and unrelated code takes its win32 branch too -- `ssl`
    reaches for a Windows certificate store that does not exist here, for one.
    Only the CLI needs to believe it is on Windows, so `cli.sys` is the seam.
    """

    def __init__(self, real: object, platform: str) -> None:
        self._real = real
        self.platform = platform

    def __getattr__(self, name: str) -> object:
        return getattr(self._real, name)


def _on_windows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, registered: bool = False
) -> _Schtasks:
    """This command believes it is on Windows, with its own definitions dir.

    `CIAO_LAUNCH_AGENTS_DIR` is the isolation override the task module already
    honours; `LOCALAPPDATA` keeps the live (unoverridden) dir inside the tmp
    tree, so a test that reaches the repoint guard cannot touch a real install.
    The backend is the real Windows one -- which backend a platform gets is
    `tests/test_service_backend.py`'s subject, and this file is about what the
    CLI asks the backend to do.
    """

    monkeypatch.setattr(cli, "sys", _PlatformShim(sys, "win32"))
    monkeypatch.setenv("CIAO_LAUNCH_AGENTS_DIR", str(tmp_path / "service"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setattr(
        service_backend, "current_backend", service_backend.WindowsBackend
    )
    monkeypatch.setattr(macos_service, "server_reachable", lambda _port, **_kw: False)
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port, **_kw: [])
    return _Schtasks(registered=registered).install(monkeypatch)


def _workspace(path: Path, *, port: int | None = None) -> Path:
    """A folder that passes the workspace checks `ciao service start --workspace` makes."""

    path.mkdir(parents=True, exist_ok=True)
    env = ["PWA_AUTH_TOKEN=test\n"]
    if port is not None:
        env.append(f"PWA_PORT={port}\n")
    (path / ".env").write_text("".join(env), encoding="utf-8")
    return path.resolve()


def _document(workspace: str) -> str:
    """A task definition naming *workspace*, in the shape the renderer writes."""

    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        f'<Task version="1.2" xmlns="{TASK_NS}">\n'
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        f"      <WorkingDirectory>{workspace}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


def _renders(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> Path:
    """Let setup write a real UTF-16 definition for the workspace it is given."""

    pythonw = tmp_path / "pythonw.exe"
    pythonw.touch()
    monkeypatch.setattr(ws, "windowless_python", lambda _python: str(pythonw))
    monkeypatch.setattr(ws, "current_user", lambda: USER)
    monkeypatch.setattr(ws, "render_task_xml", lambda **kw: _document(kw["workspace"]))
    return pythonw


def _stored_task(tmp_path: Path, workspace: Path) -> Path:
    """Write the definition a previous setup left behind for *workspace*."""

    definition = tmp_path / "service" / ws.TASK_FILE_NAME
    definition.parent.mkdir(parents=True, exist_ok=True)
    definition.write_bytes(_document(str(workspace.resolve())).encode("utf-16"))
    return definition


def test_service_status_json_reports_registered_and_reachable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """Running means the engine's own port answers; the task is `/Query`'d, never parsed."""

    _on_windows(monkeypatch, tmp_path, registered=True)
    _stored_task(tmp_path, _workspace(tmp_path / "ws", port=9443))
    probed: list[int] = []

    def reachable(port: int, **_kwargs: object) -> bool:
        probed.append(port)
        return True

    monkeypatch.setattr(macos_service, "server_reachable", reachable)

    assert cli.main(["service", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["action"] == "status"
    assert payload["details"] == {
        "registered": True,
        "reachable": True,
        "task": ws.TASK_NAME,
    }
    assert payload["message"] == "Ciaobot engine is running."
    # The port comes from the task workspace's .env, which is what setup recorded.
    assert probed == [9443]


@pytest.mark.parametrize(
    ("registered", "expected"),
    [(True, "registered but not reachable"), (False, "not registered")],
)
def test_service_status_says_why_the_engine_is_not_answering(
    registered: bool,
    expected: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _on_windows(monkeypatch, tmp_path, registered=registered)

    assert cli.main(["service", "status", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["details"]["registered"] is registered
    assert payload["details"]["reachable"] is False
    assert expected in payload["message"]
    if not registered:
        assert "ciao setup --workspace <path> --load-launchd --yes" in payload["message"]


def test_service_start_registers_the_task_for_an_unregistered_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _schtasks = _on_windows(monkeypatch, tmp_path, registered=False)
    _renders(monkeypatch, tmp_path)
    workspace = _workspace(tmp_path / "ws")

    assert cli.main(["service", "start", "--workspace", str(workspace)]) == 0

    definition = tmp_path / "service" / ws.TASK_FILE_NAME
    assert _schtasks.calls == [
        ("/Create", ws.TASK_NAME, str(definition)),
        ("/Run", ws.TASK_NAME),
    ]
    assert ws.task_workspace(definition) == workspace
    assert capsys.readouterr().out.strip() == "Ciaobot engine started."


def test_service_start_refuses_another_workspace_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The macOS guard, unchanged: a registered service is never silently repointed."""

    _schtasks = _on_windows(monkeypatch, tmp_path, registered=True)
    installed = _workspace(tmp_path / "installed")
    definition = _stored_task(tmp_path, installed)
    before = definition.read_bytes()
    requested = _workspace(tmp_path / "other")

    rc = cli.main(["service", "start", "--workspace", str(requested)])

    assert rc == 1
    message = capsys.readouterr().err
    assert f"The registered task serves {installed}, not {requested}" in message
    assert "--load-launchd --yes" in message
    assert definition.read_bytes() == before, "a refusal must not rewrite the task"
    assert _schtasks.calls == [], "a refusal must not reach the scheduler"


@pytest.mark.parametrize(
    ("action", "verbs"), [("stop", ["/End"]), ("restart", ["/End", "/Run"])]
)
def test_service_stop_and_restart_end_and_run_the_task(
    action: str,
    verbs: list[str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    _schtasks = _on_windows(monkeypatch, tmp_path, registered=True)

    assert cli.main(["service", action]) == 0

    assert _schtasks.verbs == verbs
    assert capsys.readouterr().out.strip().startswith("Ciaobot engine ")


def test_service_stop_refuses_while_a_chat_is_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """/End is a hard stop, so an active chat needs --force, exactly as on macOS."""

    _schtasks = _on_windows(monkeypatch, tmp_path, registered=True)
    monkeypatch.setattr(macos_service, "active_chat_ids", lambda _port, **_kw: ["chat-1"])

    assert cli.main(["service", "stop"]) == 1

    assert "Active chats must be confirmed" in capsys.readouterr().err
    assert _schtasks.calls == []

    assert cli.main(["service", "stop", "--force"]) == 0

    assert _schtasks.verbs == ["/End"]


@pytest.mark.parametrize("action", sorted(UNAVAILABLE_ARGV))
def test_service_desktop_actions_fail_cleanly_on_windows(
    action: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """The Mac desktop shell and the C8 updater have no Windows half: fail, change nothing."""

    _schtasks = _on_windows(monkeypatch, tmp_path, registered=True)

    assert cli.main(UNAVAILABLE_ARGV[action]) == 1

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["action"] == action
    assert "not available on Windows yet" in payload["message"]
    assert _schtasks.calls == []


def test_setup_load_launchd_registers_the_task_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`--load-launchd` writes the task XML and loads it, through the Windows backend."""

    _schtasks = _on_windows(monkeypatch, tmp_path, registered=False)
    _renders(monkeypatch, tmp_path)
    workspace = tmp_path / "ws"

    rc = cli.main(
        [
            "setup",
            "--workspace",
            str(workspace),
            "--auth-token",
            "test-token",
            "--load-launchd",
            "--yes",
        ]
    )

    definition = tmp_path / "service" / ws.TASK_FILE_NAME
    assert rc == 0
    assert definition.read_bytes().startswith(b"\xff\xfe"), "Task Scheduler wants UTF-16"
    assert ws.task_workspace(definition) == workspace.resolve()
    # WindowsBackend.load_agent registers the definition and starts the task.
    assert _schtasks.calls == [("/Create", ws.TASK_NAME, str(definition)), ("/Run", ws.TASK_NAME)]


def test_setup_without_load_launchd_prints_the_windows_next_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    _schtasks = _on_windows(monkeypatch, tmp_path, registered=False)
    _renders(monkeypatch, tmp_path)
    workspace = tmp_path / "ws"

    rc = cli.main(
        ["setup", "--workspace", str(workspace), "--auth-token", "test-token"]
    )

    assert rc == 0
    # launchctl wording would be wrong here: the task is registered by setup, not launchctl.
    assert "Task not registered. To register it: ciao setup" in capsys.readouterr().out
    assert _schtasks.calls == []


def test_setup_load_launchd_is_still_refused_on_linux(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setattr(cli, "sys", _PlatformShim(sys, "linux"))

    rc = cli.main(
        ["setup", "--workspace", str(tmp_path / "ws"), "--load-launchd"]
    )

    assert rc == 2
    assert "requires macOS or Windows" in capsys.readouterr().err
    assert not (tmp_path / "ws").exists(), "the refusal must come before any setup work"