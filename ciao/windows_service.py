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

This module also owns the lifecycle ``ciao service`` drives on Windows
(``status``, ``start``, ``stop``, ``restart``) and answers in
``macos_service.ServiceResult``, so the CLI prints one result shape on every
platform. The macOS desktop shell and the engine updater have no Windows
equivalent and are not stubbed here; the CLI refuses those outright.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from ciao import macos_service

TASK_NAME = "\\Ciaobot\\Engine"
TASK_FILE_NAME = "Ciaobot-Engine.xml"
TASK_DESCRIPTION = "Ciaobot engine: starts `ciao supervise` when this user logs on."
RESTART_INTERVAL = "PT1M"   # the schema minimum
RESTART_COUNT = 999         # the schema maximum
SCHTASKS_TIMEOUT_S = 30.0
HANDOFF_DELAY_S = 3
# The task schema's namespace: every element in the document is qualified, so a
# reader has to qualify its search the same way or it finds nothing.
TASK_NS = "{http://schemas.microsoft.com/windows/2004/02/mit/task}"
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


def task_workspace(definition: Path) -> Path | None:
    """The workspace the stored definition serves, or None if it says nothing.

    The working directory is the one value in a task definition that answers
    "which workspace is this task for?", which is what the repoint guards need.
    Unreadable or malformed XML is None rather than an error: there is no
    workspace to disagree with, and refusing to register would strand the user
    behind a corrupt file.
    """

    try:
        root = ElementTree.parse(str(definition)).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    element = root.find(
        f"{TASK_NS}Actions/{TASK_NS}Exec/{TASK_NS}WorkingDirectory"
    )
    text = (element.text or "").strip() if element is not None else ""
    return Path(text) if text else None


# Asked of the user in every place where the answer is "there is no task yet".
# One string, so `status`, `start`, `stop` and `restart` cannot drift into
# telling the same person three different ways to register it.
_NOT_REGISTERED = (
    "Ciaobot engine task is not registered. "
    "Run `ciao setup --workspace <path> --load-launchd --yes`."
)


def service_status(port: int) -> macos_service.ServiceResult:
    """Registered (a ``/Query`` exit code) and reachable (the engine's own port).

    Both facts are asked of the machine rather than parsed out of localized
    output. The caller resolves ``port`` from the task workspace's ``.env``, the
    way the macOS status resolves it from the plist.
    """

    try:
        registered = task_exists()
    except WindowsServiceError as exc:
        return _failed("status", str(exc))
    reachable = macos_service.server_reachable(port)
    if reachable:
        message = "Ciaobot engine is running."
    elif registered:
        message = f"Ciaobot engine is registered but not reachable (task {TASK_NAME})."
    else:
        message = _NOT_REGISTERED
    return macos_service.ServiceResult(
        True,
        "status",
        message,
        {"registered": registered, "reachable": reachable, "task": TASK_NAME},
    )


def start_service(workspace: Path | None = None) -> macos_service.ServiceResult:
    """Register the task for ``workspace`` when it is missing, then run it.

    The repoint refusal is the caller's: it holds both the requested workspace
    and the stored one, and refuses before anything is written here, so
    reaching this function means the workspace is allowed to be the task's.

    An already-registered task is started, never re-registered: rewriting it
    would repoint the engine at this run's interpreter behind the guard's back,
    and the guard has already established the workspace is the right one.
    """

    try:
        registered = task_exists()
    except WindowsServiceError as exc:
        return _failed("start", str(exc))
    if not registered:
        if workspace is None:
            # Nothing to register and no workspace to register for. Say so in
            # our own words rather than surfacing a localized `/Run` error.
            return _failed("start", _NOT_REGISTERED, {"setup_required": True})
        try:
            definition = write_task_definition(
                workspace=Path(workspace).expanduser().resolve(),
                python=os.environ.get("CIAO_ENGINE_PATH", "").strip() or None,
                directory=default_task_dir(),
            )
            register_task(definition)
        except (OSError, ValueError, WindowsServiceError) as exc:
            return _failed("start", str(exc))
    try:
        start_task()
    except WindowsServiceError as exc:
        return _failed("start", str(exc))
    return macos_service.ServiceResult(
        True, "start", "Ciaobot engine started.", {"task": TASK_NAME}
    )


def stop_service(port: int, *, force: bool = False) -> macos_service.ServiceResult:
    """``/End`` the task. Active chats need ``force``, as on macOS."""

    guard = _active_chat_guard(port, "stop", force)
    if guard is not None:
        return guard
    failure = _end_task("stop")
    if failure is not None:
        return failure
    return macos_service.ServiceResult(
        True, "stop", "Ciaobot engine stopped.", {"task": TASK_NAME}
    )


def restart_service(port: int, *, force: bool = False) -> macos_service.ServiceResult:
    """End the task, then run it again. Active chats need ``force``, as on macOS."""

    guard = _active_chat_guard(port, "restart", force)
    if guard is not None:
        return guard
    # A task that was not running is not an error here: the point of a restart
    # is that the engine runs afterwards, and `/Run` is what makes that true.
    failure = _end_task("restart")
    if failure is not None:
        return failure
    try:
        start_task()
    except WindowsServiceError as exc:
        return _failed("restart", str(exc))
    return macos_service.ServiceResult(
        True, "restart", "Ciaobot engine restarted.", {"task": TASK_NAME}
    )


def _end_task(action: str) -> macos_service.ServiceResult | None:
    """``/End``, tolerating a task that is registered but not running.

    ``schtasks /End`` exits non-zero when the task has no running instance, so
    "already stopped" and "no such task" look the same on the wire. ``/Query``
    tells them apart: an unregistered task is a refusal the user can act on,
    and a stopped one is the state they asked for. None means the end is done.
    """

    try:
        stop_task()
        return None
    except WindowsServiceError as exc:
        try:
            registered = task_exists()
        except WindowsServiceError:
            return _failed(action, str(exc))
        if registered:
            return None
        return _failed(action, _NOT_REGISTERED)


def _failed(
    action: str, message: str, details: dict[str, Any] | None = None
) -> macos_service.ServiceResult:
    return macos_service.ServiceResult(False, action, message, details or {})


def _active_chat_guard(port: int, action: str, force: bool) -> macos_service.ServiceResult | None:
    """A refusal while chats are active, else None. ``/End`` is a hard stop."""

    if force:
        return None
    active = macos_service.active_chat_ids(port)
    if not active:
        return None
    return macos_service.ServiceResult(
        False,
        action,
        "Active chats must be confirmed before "
        f"{'stopping' if action == 'stop' else 'restarting'} the engine.",
        {"active_chat_ids": active, "requires_confirmation": True, "task": TASK_NAME},
    )
