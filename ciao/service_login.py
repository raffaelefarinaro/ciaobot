"""Verified start-at-sign-in state and control for the host engine's service (#913).

Two questions a Settings card has to ask about *this* engine: will its service
start when this user signs in, and can that be changed from here without
touching anything else? This module answers both, and the answer is always read
from the machine rather than inferred from what Ciaobot would have installed.

Three rules hold on every platform:

* **Ownership first.** The status only answers for a service definition that
  names this engine's own workspace. A definition for another workspace is
  reported as such and never touched, and a definition that names no workspace
  is not assumed to be ours — ``macos_service.discover_runtime`` falls back to
  ``CIAO_WORKSPACE`` from the environment, and trusting that fallback would let
  a malformed plist be claimed as this engine's own.
* **Unknown is an answer.** A query that fails, a plist that does not parse, a
  ``print-disabled`` listing that cannot be read, a Task Scheduler identity
  mangled by a lossy code page: each of these reports ``installed=None`` or
  ``enabled=None`` with the reason, rather than the default that would be a
  guess. The UI can show "unknown" and the reason; it cannot show a toggle
  whose position nobody proved.
* **A change is the enabled bit, and nothing else.** macOS asks launchd to
  enable or disable the job it already has; Windows asks ``schtasks /Change`` to
  flip the registered task's enabled bit. Nothing here starts, stops, restarts,
  bootstraps, registers, writes a plist, writes task XML, or edits the stored
  definition. The write is followed by a re-read, and a change only counts as
  successful when the re-read says what was asked for.

Every OS call is a fixed argv, captured, without a shell, and bounded by a
timeout. Tests inject runners; there is no code path that reaches a live
``launchctl`` or ``schtasks.exe`` from a request.
"""

from __future__ import annotations

import plistlib
import re
import shlex
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence
from xml.etree import ElementTree
from xml.parsers.expat import ExpatError

from ciao import macos_service, windows_service

PLATFORM_MACOS = "macos"
PLATFORM_WINDOWS = "windows"
PLATFORM_LINUX = "linux"
PLATFORM_OTHER = "other"

# `launchctl` is local and answers in milliseconds; the bound is here so a
# wedged launchd surfaces as an error instead of holding a request thread.
_LAUNCHCTL_TIMEOUT_S = 10.0

# The task schema's namespace: every element in the document is qualified, so a
# search has to qualify it the same way or it finds nothing.
_TASK_NS = windows_service.TASK_NS

# schtasks prints the registered task's XML in the console's OEM code page, not
# the UTF-16 its own declaration claims (see `windows_service.task_command`).
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>")
# One `"label" => <word>` line of `launchctl print-disabled`.
_DISABLED_ENTRY = re.compile(r'"(?P<label>[^"]*)"\s*=>\s*(?P<value>[A-Za-z]+)')
# The header of the one section of that output we read. Real `launchctl
# print-disabled gui/<uid>` does not answer with a bare dictionary: it answers
# with a section, preceded by a blank line and a tab of its own —
# `\tdisabled services = {`, one entry indented a level deeper, `\t}` — and a
# launchd that also prints `login item associations = { … }` puts a second
# top-level section after it, so the last `}` in the output is not ours.
_DISABLED_SECTION_HEADER = re.compile(r"^[ \t]*disabled services[ \t]*=[ \t]*\{")
# launchd has spelled the per-label override both ways: `"label" => disabled`
# and the boolean `"label" => true` (where true means disabled).
_DISABLED_WORDS = {
    "disabled": True,
    "enabled": False,
    "true": True,
    "false": False,
}

# Fixed, and only ever shown to a human: the argv that inspects the registered
# task. Nothing in this module runs it, and nothing accepts a command from a
# caller.
_WINDOWS_INSPECT_COMMAND = f'schtasks.exe /Query /TN "{windows_service.TASK_NAME}" /XML'
_WINDOWS_REGISTER_HINT = "`ciao setup --workspace <path> --load-launchd --yes` registers it"

_LINUX_REASON = (
    "Ciaobot does not read or change an engine's start-at-sign-in state on Linux: "
    "the service is a systemd unit an administrator enables by hand (see "
    "docs/LINUX.md, `sudo systemctl enable --now ciaobot`)."
)
_OTHER_REASON = (
    "Ciaobot has no service backend for this platform, so the engine's "
    "start-at-sign-in state cannot be read or changed here."
)


@dataclass(frozen=True, slots=True)
class LoginStatus:
    """What the machine says about starting the engine at the next sign-in.

    ``installed``/``enabled`` are tri-state on purpose: ``None`` means the
    machine did not say, and a UI must not turn that into a position.
    ``can_change`` is the only field that licenses a write, and it is True only
    when ownership was proven *and* the enabled bit is the thing that decides.
    """

    platform: str
    supported: bool
    installed: bool | None
    enabled: bool | None
    can_change: bool
    reason: str
    setup_command: str | None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class LoginError(RuntimeError):
    """Base for the two ways a requested sign-in change does not happen.

    ``status`` is the freshest read available at the moment of the failure, so
    a caller can answer with the current state beside the reason.
    """

    def __init__(self, message: str, status: LoginStatus | None = None) -> None:
        super().__init__(message)
        self.status = status


class LoginRefused(LoginError):
    """The change is not this engine's to make: a refusal, not a failure."""


class LoginUnavailable(LoginError):
    """The OS would not answer, or answered something that cannot be trusted."""


# --------------------------------------------------------------------------- #
# The OS seams. Every launchctl/schtasks call this module makes goes through
# one of these four names, so a test can answer for the machine and nothing
# else in the file reaches an OS command.
# --------------------------------------------------------------------------- #


def _uid() -> int:
    """This user's uid, the ``<uid>`` in launchd's ``gui/<uid>`` domain.

    Routed through ``macos_service`` because that is where the platform
    difference already lives: ``os.getuid`` does not exist on Windows.
    """
    return macos_service._getuid()


def _launchctl_run(
    argv: Sequence[str], **kwargs: Any
) -> subprocess.CompletedProcess[str]:
    """Run one bounded ``launchctl`` call: fixed argv, captured, no shell.

    A timeout is reported the way ``windows_service._schtasks`` reports one: as
    the failed-to-run the caller already handles, not as a
    ``subprocess.TimeoutExpired`` raised out of a request thread. That matters on
    the write path too — ``macos_service.set_login_enabled`` catches ``OSError``
    and nothing else, so a launchctl that never answered would be a 500 there
    rather than the 503 this module reports for a change it cannot confirm.
    """
    options: dict[str, Any] = {
        "capture_output": True,
        "text": True,
        "check": False,
        "timeout": _LAUNCHCTL_TIMEOUT_S,
    }
    options.update(kwargs)
    try:
        return subprocess.run(list(argv), **options)
    except subprocess.TimeoutExpired as exc:
        raise OSError(
            f"launchctl {' '.join(str(arg) for arg in argv)} timed out after "
            f"{_LAUNCHCTL_TIMEOUT_S:g}s"
        ) from exc


def _query_task_xml() -> subprocess.CompletedProcess[str]:
    """Ask Task Scheduler for the *registered* engine task's definition."""
    return windows_service.query_task_xml()


def _set_task_enabled(enabled: bool) -> None:
    """Flip the registered task's enabled bit; start nothing."""
    windows_service.set_task_enabled(enabled)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _platform() -> str:
    """Which service backend this platform has. Read fresh on every call."""
    if sys.platform == "darwin":
        return PLATFORM_MACOS
    if sys.platform == "win32":
        return PLATFORM_WINDOWS
    if sys.platform.startswith("linux"):
        return PLATFORM_LINUX
    return PLATFORM_OTHER


def _resolved_root(workspace: Path) -> Path:
    """The requested workspace, resolved the way the repoint guards resolve it.

    A path that cannot be resolved is compared un-resolved rather than dropped:
    the comparison below is what proves ownership, and silently comparing
    nothing would be the guess this module refuses to make.
    """
    path = Path(workspace).expanduser()
    try:
        return path.resolve()
    except OSError:
        return path


def _unsupported_status() -> LoginStatus:
    """Linux and the platforms with no backend: a stated answer, no OS call.

    The caller must not reach the filesystem or a subprocess on these branches
    at all, so nothing here does.
    """
    reason = _LINUX_REASON if _platform() == PLATFORM_LINUX else _OTHER_REASON
    return LoginStatus(
        platform=_platform(),
        supported=False,
        installed=None,
        enabled=None,
        can_change=False,
        reason=reason,
        setup_command=None,
    )


def _require_changeable(before: LoginStatus) -> None:
    """Refuse anything that is not a proven, changeable enabled bit."""
    if before.can_change and before.enabled is not None:
        return
    raise LoginRefused(before.reason, before)


def _state_reason(enabled: bool) -> str:
    """The one sentence a proven state is reported with, on every platform."""
    return (
        "The engine service will start when this user signs in."
        if enabled
        else "The engine service will not start when this user signs in."
    )


# --------------------------------------------------------------------------- #
# macOS: the LaunchAgent launchd already has
# --------------------------------------------------------------------------- #


def _plist_path() -> Path:
    """The real per-user LaunchAgents plist for the engine service.

    ``live_launch_agents_dir`` and not ``default_launch_agents_dir``:
    ``CIAO_LAUNCH_AGENTS_DIR`` is a test/install override, and the definition
    launchd loads at the next sign-in is the one in the real per-user dir.
    Reading the override would describe an install that is not running.
    """
    return (
        macos_service.live_launch_agents_dir()
        / f"{macos_service.SERVER_LABEL}.plist"
    )


def _load_plist(path: Path) -> dict[str, Any] | None:
    """The plist as a dictionary, or None when it is not one we can read.

    ``ExpatError`` is caught alongside ``InvalidFileException`` because a
    truncated or corrupt property list is an expat failure, not one of the
    "this is not a plist" cases plistlib raises itself — and this read has to
    end in a reason a user can act on, not a traceback.
    """
    try:
        with path.open("rb") as handle:
            loaded = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException, ExpatError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def _declared_workspaces(plist: dict[str, Any]) -> list[str]:
    """The workspaces the definition itself names, in its own words.

    ``EnvironmentVariables/CIAO_WORKSPACE`` and ``WorkingDirectory`` are both
    asked for, and both have to agree: a definition whose two workspace values
    disagree with each other is not an install this engine can claim.
    """
    values: list[str] = []
    env = plist.get("EnvironmentVariables")
    if isinstance(env, dict):
        declared = env.get("CIAO_WORKSPACE")
        if isinstance(declared, str) and declared.strip():
            values.append(declared.strip())
    workdir = plist.get("WorkingDirectory")
    if isinstance(workdir, str) and workdir.strip():
        values.append(workdir.strip())
    return values


def _macos_ownership_reason(plist: dict[str, Any], requested: Path) -> str:
    """Why this definition is not provably this engine's, or "" when it is."""
    declared = _declared_workspaces(plist)
    if not declared:
        return (
            "The engine LaunchAgent names no workspace (neither "
            "WorkingDirectory nor CIAO_WORKSPACE), so it cannot be shown to "
            f"serve this engine at {requested}."
        )
    for value in declared:
        served = _resolved_root(Path(value))
        if served != requested:
            return (
                f"The engine LaunchAgent serves {served}, not {requested}. This "
                "control only changes the service for this workspace; it never "
                "repoints another workspace's engine."
            )
    return ""


def _starts_at_sign_in(plist: dict[str, Any]) -> bool:
    """Whether launchd would launch the job when it loads the definition.

    A definition with neither key is a hand-written one, and the enable bit
    would not start the engine at the next sign-in no matter what it says.
    That is not a switch this module may report as On.
    """
    return plist.get("RunAtLoad") is True or bool(plist.get("KeepAlive"))


def _disabled_section(stdout: str) -> str | None:
    """The text between ``disabled services = {`` and the brace closing it.

    Braces are counted from that header so the section ends at *its own* closing
    brace: when launchd prints a following ``login item associations = { … }``,
    that dictionary's brace is not allowed to close this one, and none of its
    entries reach the parser (its values are opaque object references, not
    launchd's ``enabled``/``disabled`` words). Whatever launchd prints after the
    section is a section of its own and is left there.

    Output with no such header is not read. A ``{ … }`` body with no header is
    not a shape macOS launchd emits, and reading one would report a sign-in
    state from output this reader cannot account for.

    None means there is no complete section here, which is not the same as an
    empty one: output with no header, or an opening brace whose section never
    closes, is unreadable, not "nothing is disabled".
    """
    lines = stdout.splitlines()
    for index, line in enumerate(lines):
        header = _DISABLED_SECTION_HEADER.match(line)
        if header is None:
            continue
        body: list[str] = []
        # The header's own brace is the one outstanding; anything a `}` closes
        # from here is the section's, and a nested dictionary balances before
        # it instead of ending the section.
        depth = 0
        for entry in [line[header.end() :], *lines[index + 1 :]]:
            closing = -1
            for column, character in enumerate(entry):
                if character == "{":
                    depth += 1
                elif character == "}":
                    depth -= 1
                    if depth < 0:
                        closing = column
                        break
            if closing < 0:
                body.append(entry)
                continue
            # This line closed the section: keep everything before the brace
            # that closed it, and refuse anything left on the line after it.
            body.append(entry[:closing])
            return None if entry[closing + 1 :].strip() else "\n".join(body)
        return None
    # No header anywhere in the output. That is not a listing in a shape this
    # reader accepts, and it is not an empty one either: it is unreadable.
    return None


def _parse_disabled_listing(stdout: str) -> dict[str, bool] | None:
    """``label -> disabled`` from ``launchctl print-disabled``, or None.

    Only launchd's own ``disabled services`` section is read (see
    ``_disabled_section``), and None means "this output cannot be read", which is
    not the same as "nothing is disabled": output that carries no such section —
    a bare ``{ … }`` body, an unrelated line, nothing at all — is not a listing
    we may read, and neither is a section that never closes, a leftover line
    inside one, a value that is neither spelling launchd has used, or one label
    listed twice with two different answers. All of them mean the answer is
    unknown, and the caller reports that instead of guessing a position.
    """
    body = _disabled_section(stdout)
    if body is None:
        return None
    entries = _DISABLED_ENTRY.findall(body)
    # Every line has to be an entry this understands. A listing with a line
    # left over is a listing that may be hiding our own label in it.
    if _DISABLED_ENTRY.sub("", body).replace(",", "").strip():
        return None
    states: dict[str, bool] = {}
    for label, value in entries:
        word = value.strip().lower()
        if word not in _DISABLED_WORDS:
            return None
        disabled = _DISABLED_WORDS[word]
        if states.get(label, disabled) != disabled:
            return None
        states[label] = disabled
    return states


def _disabled_overrides(uid: int) -> tuple[dict[str, bool] | None, str]:
    """launchd's per-label overrides, and why they are unreadable.

    ``TimeoutExpired`` is caught alongside ``OSError`` because this read answers
    a status: a launchctl that exceeded the bound is a question the machine did
    not answer, not a failure the request may raise. ``_launchctl_run`` already
    turns it into an ``OSError``, and it is caught here too because the seam this
    call goes through is a runner argument any caller can supply.
    """
    domain = f"gui/{uid}"
    try:
        completed = macos_service._launchctl(
            ["print-disabled", domain], runner=_launchctl_run
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"launchctl print-disabled {domain} could not be run: {exc}"
    if completed.returncode != 0:
        detail = macos_service._command_error(completed) or f"exit {completed.returncode}"
        return None, f"launchctl print-disabled {domain} failed: {detail}"
    listing = _parse_disabled_listing(completed.stdout or "")
    if listing is None:
        return None, f"launchctl print-disabled {domain} returned output that cannot be read"
    return listing, ""


def _macos_status(workspace: Path) -> LoginStatus:
    """macOS: what launchd will do with the engine's own LaunchAgent."""
    platform = PLATFORM_MACOS
    requested = _resolved_root(workspace)
    plist_path = _plist_path()
    if not plist_path.is_file():
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=False,
            enabled=None,
            can_change=False,
            reason=(
                "The Ciaobot engine service is not installed for this user, so "
                "there is nothing to start at sign-in yet."
            ),
            setup_command=(
                f"ciao service start --workspace {shlex.quote(str(requested))}"
            ),
        )
    plist = _load_plist(plist_path)
    if plist is None:
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=None,
            enabled=None,
            can_change=False,
            reason=(
                f"{plist_path} could not be read as a plist dictionary, so its "
                "sign-in state is unknown."
            ),
            setup_command=None,
        )
    if plist.get("Label") != macos_service.SERVER_LABEL:
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=None,
            enabled=None,
            can_change=False,
            reason=(
                f"{plist_path} declares Label {plist.get('Label')!r}, not "
                f"{macos_service.SERVER_LABEL}: it is not this engine's service."
            ),
            setup_command=None,
        )
    ownership = _macos_ownership_reason(plist, requested)
    if ownership:
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=True,
            enabled=None,
            can_change=False,
            reason=ownership,
            setup_command=None,
        )
    if not _starts_at_sign_in(plist):
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=True,
            enabled=None,
            can_change=False,
            reason=(
                f"{plist_path} sets neither RunAtLoad nor KeepAlive, so launchd "
                "would not start the engine at the next sign-in whatever its "
                "enable bit says. This definition was configured by hand."
            ),
            setup_command=None,
        )
    listing, error = _disabled_overrides(_uid())
    if listing is None:
        return LoginStatus(
            platform=platform,
            supported=True,
            installed=True,
            enabled=None,
            can_change=False,
            reason=f"The engine service is installed, but its sign-in state is unknown: {error}.",
            setup_command=None,
        )
    label = macos_service.SERVER_LABEL
    if label in listing:
        enabled = not listing[label]
    else:
        # No override for our label: the plist's own Disabled key decides, and
        # launchd's default for a key that is absent is false (not disabled).
        # A disabled *job* says nothing here — the question is the next
        # sign-in, which a running agent has already had.
        disabled = plist.get("Disabled")
        if not isinstance(disabled, bool) and disabled is not None:
            return LoginStatus(
                platform=platform,
                supported=True,
                installed=True,
                enabled=None,
                can_change=False,
                reason=(
                    f"{plist_path} has a Disabled key of {disabled!r}, which is "
                    "not a boolean, so its sign-in state is unknown."
                ),
                setup_command=None,
            )
        enabled = not bool(disabled)
    return LoginStatus(
        platform=platform,
        supported=True,
        installed=True,
        enabled=enabled,
        can_change=True,
        reason=_state_reason(enabled),
        setup_command=None,
    )


def _macos_runtime(
    plist: dict[str, Any], plist_path: Path, workspace: Path
) -> macos_service.DesktopRuntime:
    """The runtime the validated definition describes, for the enable call.

    Only ``server_plist`` is load-bearing: ``macos_service.set_login_enabled``
    re-checks that the validated definition is still a file before it asks
    launchd anything. The rest keeps the result message honest about which
    workspace and program this is.

    ``python_path`` is the interpreter the definition runs. A hosted
    definition runs ``CiaobotServerHost serve --python <interpreter>``, so the
    host executable must not be reported as the interpreter; the served python
    is resolved by the shared recognition in
    ``macos_service._service_python_path`` (through ``parse_service_command``),
    and an unparsed legacy direct argv keeps today's argv[0] behavior.
    """
    arguments = plist.get("ProgramArguments")
    python_path = macos_service._service_python_path(arguments)
    return macos_service.DesktopRuntime(
        workspace=str(workspace),
        runtime_root=str(workspace / ".runtime"),
        port=macos_service.DEFAULT_PORT,
        server_plist=str(plist_path),
        python_path=python_path,
    )


def _set_macos(workspace: Path, enabled: bool) -> LoginStatus:
    """macOS: re-check, ask launchd for the enabled bit, then re-read it."""
    before = _macos_status(workspace)
    _require_changeable(before)
    plist_path = _plist_path()
    plist = _load_plist(plist_path)
    if plist is None:  # pragma: no cover - the status read just parsed it
        raise LoginUnavailable(
            f"{plist_path} became unreadable between the check and the change.",
            before,
        )
    result = macos_service.set_login_enabled(
        enabled,
        runtime=_macos_runtime(plist, plist_path, _resolved_root(workspace)),
        uid=_uid(),
        runner=_launchctl_run,
    )
    if not result.ok:
        raise LoginUnavailable(
            f"launchctl did not change the engine's sign-in state: {result.message}",
            _macos_status(workspace),
        )
    after = _macos_status(workspace)
    if after.enabled != enabled:
        raise LoginUnavailable(
            "launchctl reported the change, but the re-read sign-in state is "
            f"{'unknown' if after.enabled is None else after.enabled}.",
            after,
        )
    return after


# --------------------------------------------------------------------------- #
# Windows: the registered Task Scheduler task
# --------------------------------------------------------------------------- #


def _windows_unreadable(reason: str) -> LoginStatus:
    """Nothing usable came back from ``/Query /XML``.

    ``installed`` stays None on purpose: a failed query, a localized "no such
    task" and a permission error all look the same on the wire, and answering
    "not installed" from one of them would be a guess the user would act on.
    """
    return LoginStatus(
        platform=PLATFORM_WINDOWS,
        supported=True,
        installed=None,
        enabled=None,
        can_change=False,
        reason=reason,
        setup_command=_WINDOWS_INSPECT_COMMAND,
    )


def _windows_installed(reason: str) -> LoginStatus:
    """A task is registered, but the sign-in state could not be proven."""
    return LoginStatus(
        platform=PLATFORM_WINDOWS,
        supported=True,
        installed=True,
        enabled=None,
        can_change=False,
        reason=reason,
        setup_command=None,
    )


def _has_enabled_logon_trigger(root: ElementTree.Element) -> bool:
    """Whether the definition fires at logon with its trigger enabled."""
    triggers = root.find(f"{_TASK_NS}Triggers")
    if triggers is None:
        return False
    for trigger in triggers.findall(f"{_TASK_NS}LogonTrigger"):
        node = trigger.find(f"{_TASK_NS}Enabled")
        if node is not None and (node.text or "").strip().lower() == "true":
            return True
    return False


def _windows_status(workspace: Path) -> LoginStatus:
    """Windows: what the registered engine task's own definition says."""
    requested = _resolved_root(workspace)
    try:
        completed = _query_task_xml()
    except windows_service.WindowsServiceError as exc:
        return _windows_unreadable(
            f"The engine task's definition could not be read from Task Scheduler: {exc}"
        )
    if completed.returncode != 0:
        detail = (
            (completed.stderr or completed.stdout or "").strip()
            or f"exit {completed.returncode}"
        )
        return _windows_unreadable(
            f"Task Scheduler did not return the engine task's definition: {detail}. "
            f"{_WINDOWS_REGISTER_HINT}."
        )
    # The declaration names an encoding the text no longer has.
    body = _XML_DECLARATION.sub("", completed.stdout or "")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        return _windows_unreadable(
            f"The engine task's definition is not XML this can read: {exc}"
        )
    if root.tag != f"{_TASK_NS}Task":
        return _windows_unreadable(
            "What Task Scheduler returned for the engine task is not a Task "
            "Scheduler task document."
        )
    actions = root.find(f"{_TASK_NS}Actions")
    execs = actions.findall(f"{_TASK_NS}Exec") if actions is not None else []
    if len(execs) != 1:
        return _windows_installed(
            f"The engine task has {len(execs)} Exec actions instead of one, so "
            "there is no single workspace to read it from."
        )
    node = execs[0].find(f"{_TASK_NS}WorkingDirectory")
    workdir = (node.text or "").strip() if node is not None else ""
    if not workdir:
        return _windows_installed(
            "The engine task's action names no working directory, so it cannot "
            "be shown to serve this workspace."
        )
    # A `?` or a non-ASCII character means the OEM code page lost or
    # best-fit-mapped the real path. Trusting it would compare a path the
    # machine never stored.
    if "?" in workdir or not workdir.isascii():
        return _windows_installed(
            "The engine task's working directory came back through a lossy code "
            "page, so it cannot be compared with this workspace."
        )
    served = _resolved_root(Path(workdir))
    if served != requested:
        return _windows_installed(
            f"The engine task serves {served}, not {requested}. This control "
            "only changes the task for this workspace; it never repoints another "
            "workspace's engine."
        )
    settings = root.find(f"{_TASK_NS}Settings")
    enabled_node = settings.find(f"{_TASK_NS}Enabled") if settings is not None else None
    declared = (enabled_node.text or "").strip().lower() if enabled_node is not None else ""
    if declared not in {"true", "false"}:
        return _windows_installed(
            "The engine task's Settings/Enabled is not an explicit true or "
            "false, so its sign-in state is unknown."
        )
    if not _has_enabled_logon_trigger(root):
        return _windows_installed(
            "The engine task has no enabled logon trigger, so enabling the task "
            f"would not start the engine at the next sign-in. {_WINDOWS_REGISTER_HINT}."
        )
    enabled = declared == "true"
    return LoginStatus(
        platform=PLATFORM_WINDOWS,
        supported=True,
        installed=True,
        enabled=enabled,
        can_change=True,
        reason=_state_reason(enabled),
        setup_command=None,
    )


def _set_windows(workspace: Path, enabled: bool) -> LoginStatus:
    """Windows: re-check, flip the registered enabled bit, then re-read it."""
    before = _windows_status(workspace)
    _require_changeable(before)
    try:
        _set_task_enabled(enabled)
    except windows_service.WindowsServiceError as exc:
        raise LoginUnavailable(
            f"Task Scheduler did not change the engine task's sign-in state: {exc}",
            _windows_status(workspace),
        ) from exc
    after = _windows_status(workspace)
    if after.enabled != enabled:
        raise LoginUnavailable(
            "Task Scheduler reported the change, but the re-read sign-in state is "
            f"{'unknown' if after.enabled is None else after.enabled}.",
            after,
        )
    return after


# --------------------------------------------------------------------------- #
# The public entry points
# --------------------------------------------------------------------------- #


def login_status(workspace: Path) -> LoginStatus:
    """Whether the engine service for *workspace* starts at the next sign-in.

    Read-only: it never starts, stops, restarts, bootstraps or registers
    anything, and it never writes a plist or task XML. The only OS calls it
    makes are a plist read and (on macOS) ``launchctl print-disabled``.
    """
    platform = _platform()
    if platform == PLATFORM_MACOS:
        return _macos_status(Path(workspace))
    if platform == PLATFORM_WINDOWS:
        return _windows_status(Path(workspace))
    return _unsupported_status()


def set_login_enabled(workspace: Path, enabled: bool) -> LoginStatus:
    """Set the engine service's start-at-sign-in state, and verify the result.

    Re-checks ownership and changeability immediately before the write, so a
    state that changed between the caller's read and this call is refused
    rather than acted on, and re-reads the machine afterwards: a change counts
    as done only when the re-read says what was asked for.
    """
    platform = _platform()
    if platform == PLATFORM_MACOS:
        return _set_macos(Path(workspace), enabled)
    if platform == PLATFORM_WINDOWS:
        return _set_windows(Path(workspace), enabled)
    raise LoginRefused(_unsupported_status().reason, login_status(workspace))
