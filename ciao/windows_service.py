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
platform, and it renders the engine updater's two sibling tasks, which
``ciao.windows_update`` registers (#857). The macOS desktop shell has no
Windows equivalent and is not stubbed here; the CLI refuses it outright.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Sequence
from xml.etree import ElementTree
from xml.sax.saxutils import escape

from ciao import macos_service
from ciao.os_support.users import user_key

TASK_NAME = "\\Ciaobot\\Engine"
TASK_FILE_NAME = "Ciaobot-Engine.xml"
TASK_DESCRIPTION = "Ciaobot engine: starts `ciao supervise` when this user logs on."
RESTART_INTERVAL = "PT1M"   # the schema minimum
RESTART_COUNT = 999         # the schema maximum
SCHTASKS_TIMEOUT_S = 30.0
HANDOFF_DELAY_S = 3
# The engine updater's two sibling tasks (#857). The updater runs one swap and
# is bounded well past a stop, an offline install and a readiness wait; the
# recovery task re-checks a stranded swap once a minute.
UPDATER_TASK_NAME = "\\Ciaobot\\Updater"
UPDATER_DESCRIPTION = "Ciaobot engine update: replaces the engine, then exits."
RECOVER_TASK_NAME = "\\Ciaobot\\Recover"
RECOVER_DESCRIPTION = "Ciaobot engine update recovery: rolls back an interrupted update."
RECOVER_INTERVAL = "PT1M"
UPDATE_JOB_TIME_LIMIT = "PT30M"
_LOCAL_TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")
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


# One document for every task Ciaobot registers: the engine's logon task and
# the updater's two siblings (#857). They share the principal and the settings
# on purpose, and differ only in their triggers, their time limit, whether a
# failure is retried, and the program they run.
_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{description}</Description>
    <URI>{task_name}</URI>
  </RegistrationInfo>
{triggers}  <Principals>
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
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
{restart}  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

_LOGON_TRIGGER = """\
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{user}</UserId>
    </LogonTrigger>
"""

# No <Duration>: the repetition runs for as long as the trigger is enabled,
# across reboots, which is what the recovery task needs. It is retired by
# deleting the task, not by letting the repetition run out.
_REPEATING_TRIGGER = """\
    <TimeTrigger>
      <Repetition>
        <Interval>{interval}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
"""

_RESTART_ON_FAILURE = """\
    <RestartOnFailure>
      <Interval>{interval}</Interval>
      <Count>{count}</Count>
    </RestartOnFailure>
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
    user_text = _xml_text("user", user)
    return _TEMPLATE.format(
        description=escape(TASK_DESCRIPTION),
        task_name=escape(TASK_NAME),
        triggers=_triggers(_LOGON_TRIGGER.format(user=user_text)),
        user=user_text,
        time_limit="PT0S",
        restart=_RESTART_ON_FAILURE.format(interval=RESTART_INTERVAL, count=RESTART_COUNT),
        python=_windows_absolute("python", python),
        arguments="-m ciao.cli supervise",
        workdir=_windows_absolute("workspace", workspace),
    )


def _triggers(*blocks: str) -> str:
    return "  <Triggers>\n" + "".join(blocks) + "  </Triggers>\n"


def _update_job_xml(
    *,
    task_name: str,
    description: str,
    triggers: str,
    python: str,
    arguments: Sequence[str],
    workdir: str,
    user: str,
) -> str:
    return _TEMPLATE.format(
        description=escape(description),
        task_name=escape(task_name),
        triggers=triggers,
        user=_xml_text("user", user),
        time_limit=UPDATE_JOB_TIME_LIMIT,
        # A failed swap is settled by its own rollback and recorded; Task
        # Scheduler running it again would be a second swap nobody asked for.
        restart="",
        python=_windows_absolute("python", python),
        arguments=_xml_text("arguments", subprocess.list2cmdline(list(arguments))),
        workdir=_windows_absolute("workdir", workdir),
    )


def render_oneshot_task_xml(
    *, python: str, arguments: Sequence[str], workdir: str, user: str
) -> str:
    """Task XML for the updater task: no trigger, started once with ``/Run``.

    The engine task's principal and settings, so it runs as the same user in
    the same session, but as a sibling of the engine task rather than a child:
    ``/End`` on the engine, and the Job Object that kills the engine's process
    tree, cannot reach it.
    """
    return _update_job_xml(
        task_name=UPDATER_TASK_NAME,
        description=UPDATER_DESCRIPTION,
        triggers="",
        python=python,
        arguments=arguments,
        workdir=workdir,
        user=user,
    )


def render_recover_task_xml(
    *, python: str, arguments: Sequence[str], workdir: str, user: str, start: str
) -> str:
    """Task XML for the recovery task: at logon, then once a minute.

    ``start`` is the local ``YYYY-MM-DDTHH:MM:SS`` the repetition counts from.
    One minute is the schema's minimum interval (#857, maintainer decision 2).
    """
    if not _LOCAL_TIME.fullmatch(start):
        raise ValueError(f"start must be a local YYYY-MM-DDTHH:MM:SS time: {start!r}")
    user_text = _xml_text("user", user)
    return _update_job_xml(
        task_name=RECOVER_TASK_NAME,
        description=RECOVER_DESCRIPTION,
        triggers=_triggers(
            _LOGON_TRIGGER.format(user=user_text),
            _REPEATING_TRIGGER.format(interval=RECOVER_INTERVAL, start=start),
        ),
        python=python,
        arguments=arguments,
        workdir=workdir,
        user=user,
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
    """The logged-on user for a task's principal and logon trigger.

    The SID of the account this process runs as, which Task Scheduler accepts
    and resolves itself. ``USERDOMAIN\\USERNAME`` is what a desktop session
    reports, but an OpenSSH session reports ``USERDOMAIN=WORKGROUP`` for a local
    account, and `/Create` then fails with "No mapping between account names and
    security IDs was done".
    """
    try:
        return user_key()
    except OSError as exc:
        raise WindowsServiceError(f"could not read this account's SID: {exc}") from exc


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


def _schtasks(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess[str]:
    argv = ["schtasks.exe", *args]
    try:
        return subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            encoding=encoding,
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


Schtasks = Callable[..., "subprocess.CompletedProcess[str]"]


def task_command(name: str, *, runner: Schtasks = _schtasks) -> str | None:
    """The program the *registered* task runs, or None if that cannot be told.

    Asked of Task Scheduler (``/Query /XML``) rather than read from the file
    Ciaobot wrote, because the registered copy is the one that runs. schtasks
    prints that XML in the console's OEM code page, not the UTF-16 its own
    declaration claims. A character the code page lacks comes back as ``?``,
    or best-fit mapped to a look-alike (``Łukasz`` as ``Lukasz``) with nothing
    to show it happened. So only a pure-ASCII command is trusted: anything else
    answers None, which is evidence of nothing, rather than a wrong path that
    would refuse an update as "the service runs a different env".
    """
    try:
        completed = runner("/Query", "/TN", name, "/XML", encoding="oem")
    except WindowsServiceError:
        return None
    if completed.returncode != 0:
        return None
    # The declaration names an encoding the text no longer has.
    body = re.sub(r"^\s*<\?xml[^>]*\?>", "", completed.stdout or "")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return None
    element = root.find(f"{TASK_NS}Actions/{TASK_NS}Exec/{TASK_NS}Command")
    command = (element.text or "").strip() if element is not None else ""
    if not command or "?" in command or not command.isascii():
        return None
    return command


def query_task_xml(
    name: str = TASK_NAME, *, runner: Schtasks = _schtasks
) -> subprocess.CompletedProcess[str]:
    """The *registered* task's XML, as Task Scheduler prints it.

    The raw result rather than a parsed answer, because a caller that needs the
    document has to be able to tell "the query failed" (which a missing task, a
    denied access and a timeout all look like on the wire) from "the document
    says something this caller cannot use". ``task_command`` answers one string
    and so has no reason to keep them apart; ``ciao/service_login.py`` does.

    The caller strips the XML declaration: schtasks prints in the console's OEM
    code page, not the UTF-16 the declaration claims.
    """
    return runner("/Query", "/TN", name, "/XML", encoding="oem")


def set_task_enabled(
    enabled: bool, name: str = TASK_NAME, *, runner: Schtasks = _schtasks
) -> None:
    """Flip the registered task's enabled bit with ``/Change``.

    The narrowest change Task Scheduler offers: it writes the enabled bit and
    nothing else. It does not register, rewrite, run or end the task, so the
    stored definition and the task's triggers survive untouched, and no engine
    process is started or stopped by it. That is why this, and not ``/Run`` or
    a re-``/Create``, is what the start-at-sign-in control is built on.
    """
    completed = runner("/Change", "/TN", name, "/Enable" if enabled else "/Disable")
    if completed.returncode != 0:
        raise _failure("/Change", completed)


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
