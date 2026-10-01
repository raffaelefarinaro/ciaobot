"""Per-user Task Scheduler logon task that runs ``ciao supervise`` on Windows.

Task Scheduler cannot ask a task to start without a console, so a task whose
action is ``python.exe`` puts a window on the user's desktop. The renderer
therefore points ``<Exec><Command>`` at ``pythonw.exe`` next to the running
interpreter, and ``ciao supervise`` opens ``{workspace}/.runtime/ciao.std*.log``
when it finds ``sys.stderr is None`` (the pythonw condition), so the engine's
output is not lost with the console that no longer exists.

``schtasks`` output is localized, so nothing here parses it: the presence of a
task is the exit code of ``/Query`` and the health of the engine is the engine's
own port, probed by the caller. Every call is an argv list with a timeout, never
a shell.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from xml.sax.saxutils import escape

TASK_NAME = "\\Ciaobot\\Engine"
TASK_FILE_NAME = "Ciaobot-Engine.xml"
TASK_DESCRIPTION = "Ciaobot engine: starts `ciao supervise` when this user logs on."
RESTART_INTERVAL = "PT1M"   # the schema minimum
RESTART_COUNT = 999         # the schema maximum
SCHTASKS_TIMEOUT_S = 30.0
HANDOFF_DELAY_S = 3
# These three are `subprocess` attributes on Windows only. Reading them through
# getattr keeps mypy (and the tests, which run on POSIX) honest about the
# literal values rather than pretending the platform provides them.
_CREATE_NO_WINDOW = 0x08000000
_DETACHED_PROCESS = 0x00000008
_CREATE_NEW_PROCESS_GROUP = 0x00000200


class WindowsServiceError(RuntimeError):
    """A ``schtasks.exe`` call failed, timed out, or could not start."""


_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>{task_name}</URI>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
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
      <Interval>{restart_interval}</Interval>
      <Count>{restart_count}</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>-m ciao.cli supervise</Arguments>
      <WorkingDirectory>{workspace}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _xml_text(label: str, value: str) -> str:
    if not value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must be non-empty and free of control characters")
    return escape(value)


def _windows_absolute(label: str, value: str) -> str:
    path = PureWindowsPath(value)
    # A UNC path has both a drive and a root, but Task Scheduler does not
    # reliably honour one for WorkingDirectory or Command, and a task whose
    # command cannot be found fails only at logon. Rejected here, where the
    # mistake is visible, rather than there. PureWindowsPath normalises the
    # `//server/share` spelling to `\\server\share`, so the drive is the one to
    # look at: a value that only starts with backslashes after parsing is UNC.
    if path.drive.startswith("\\\\") or not (path.drive and path.root):
        raise ValueError(f"{label} must be an absolute Windows path with a drive: {value!r}")
    return _xml_text(label, value)


def render_task_xml(*, python: str, workspace: str, user: str) -> str:
    """Task Scheduler XML for the engine's logon task.

    ``python`` is the windowless interpreter (see ``windowless_python``),
    ``workspace`` the working directory, ``user`` ``DOMAIN\\name``. Values are
    XML-escaped; control characters and non-absolute paths raise ``ValueError``.
    Paths are ``PureWindowsPath``-checked so the renderer is testable anywhere.
    """
    return _TEMPLATE.format(
        description=escape(TASK_DESCRIPTION),
        task_name=escape(TASK_NAME),
        user=_xml_text("user", user),
        restart_interval=RESTART_INTERVAL,
        restart_count=RESTART_COUNT,
        python=_windows_absolute("python", python),
        workspace=_windows_absolute("workspace", workspace),
    )


def windowless_python(python: str) -> str:
    """``pythonw.exe`` next to ``python.exe`` (or ``python`` if already pythonw)."""
    path = PureWindowsPath(python)
    name = path.name.lower()
    if name == "pythonw.exe":
        return str(path)
    if name != "python.exe":
        raise ValueError(f"Expected python.exe or pythonw.exe, got {path.name!r}")
    return str(path.with_name("pythonw.exe"))


def current_user() -> str:
    """``DOMAIN\\name`` for the logged-on user, as Task Scheduler wants it."""
    name = os.environ.get("USERNAME", "").strip()
    if not name:
        raise WindowsServiceError("USERNAME is not set; cannot name the task's user.")
    domain = os.environ.get("USERDOMAIN", "").strip()
    return f"{domain}\\{name}" if domain else name


def default_task_dir() -> Path:
    """Where the task XML is written; ``CIAO_LAUNCH_AGENTS_DIR`` overrides it (tests)."""
    override = os.environ.get("CIAO_LAUNCH_AGENTS_DIR", "").strip()
    return Path(override).expanduser() if override else live_task_dir()


def live_task_dir() -> Path:
    base = os.environ.get("LOCALAPPDATA", "").strip()
    root = Path(base) if base else Path.home() / "AppData" / "Local"
    return root / "Ciaobot" / "service"


def write_task_definition(*, workspace: Path, python: str | None = None,
                          directory: Path | None = None) -> Path:
    """Render and write the task XML (UTF-16 with BOM); return its path.

    ``python`` defaults to the running interpreter, i.e. the uv tool environment
    at setup time. Raises ``WindowsServiceError`` when ``pythonw.exe`` is missing.
    """
    pythonw = windowless_python(python or sys.executable)
    if not Path(pythonw).is_file():
        raise WindowsServiceError(f"{pythonw} does not exist; cannot register a hidden task.")
    xml = render_task_xml(python=pythonw, workspace=str(workspace), user=current_user())
    target = (directory or default_task_dir()) / TASK_FILE_NAME
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(xml.encode("utf-16"))
    return target


def _schtasks(*args: str) -> subprocess.CompletedProcess[str]:
    argv = ["schtasks.exe", *args]
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SCHTASKS_TIMEOUT_S,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW),
        )
    except subprocess.TimeoutExpired as exc:
        raise WindowsServiceError(f"schtasks {args[0]} timed out after {SCHTASKS_TIMEOUT_S:g}s") from exc
    except OSError as exc:
        raise WindowsServiceError(f"Could not run schtasks.exe: {exc}") from exc


def _failure(action: str, completed: subprocess.CompletedProcess[str]) -> WindowsServiceError:
    detail = (completed.stderr or completed.stdout or "").strip() or f"exit {completed.returncode}"
    return WindowsServiceError(f"schtasks {action} failed: {detail}")


def task_exists(name: str = TASK_NAME) -> bool:
    return _schtasks("/Query", "/TN", name).returncode == 0


def register_task(definition: Path, name: str = TASK_NAME) -> None:
    completed = _schtasks("/Create", "/TN", name, "/XML", str(definition), "/F")
    if completed.returncode != 0:
        raise _failure("/Create", completed)


def unregister_task(name: str = TASK_NAME) -> bool:
    """Delete the task; False when it was not registered. No-op, not an error."""
    if not task_exists(name):
        return False
    completed = _schtasks("/Delete", "/TN", name, "/F")
    if completed.returncode != 0:
        raise _failure("/Delete", completed)
    return True


def start_task(name: str = TASK_NAME) -> None:
    completed = _schtasks("/Run", "/TN", name)
    if completed.returncode != 0:
        raise _failure("/Run", completed)


def stop_task(name: str = TASK_NAME) -> None:
    """``/End`` terminates the running task (a hard stop; the job object kills the engine tree)."""
    completed = _schtasks("/End", "/TN", name)
    if completed.returncode != 0:
        raise _failure("/End", completed)


def spawn_delayed_start(name: str = TASK_NAME) -> bool:
    """Detached helper: wait HANDOFF_DELAY_S, then ``schtasks /Run``.

    False on any failure, including an interpreter that is not a ``python*.exe``:
    the caller (``WindowsBackend.schedule_server_handoff``) is documented to
    return a bool, not to raise.
    """
    script = (
        "import subprocess, sys, time\n"
        f"time.sleep({HANDOFF_DELAY_S})\n"
        f"subprocess.run(['schtasks.exe', '/Run', '/TN', {name!r}], check=False)\n"
    )
    flags = getattr(subprocess, "DETACHED_PROCESS", _DETACHED_PROCESS) \
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", _CREATE_NEW_PROCESS_GROUP) \
        | getattr(subprocess, "CREATE_NO_WINDOW", _CREATE_NO_WINDOW)
    try:
        interpreter = windowless_python(sys.executable)
        subprocess.Popen(
            [interpreter, "-c", script],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=flags, close_fds=True,
        )
    except (OSError, ValueError):
        return False
    return True
