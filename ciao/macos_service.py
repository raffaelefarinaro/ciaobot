"""UI-independent macOS service control for the Ciaobot desktop shell."""

from __future__ import annotations

import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Sequence
from xml.parsers.expat import ExpatError

from ciao.os_support.definition import (
    MAX_DEFINITION_BYTES,
    read_definition,
)
from ciao.port_in_use import (
    PORT_IN_USE_EXIT_CODE,
    clear_port_in_use,
    port_in_use_message,
    read_port_in_use,
)
from ciao.server_host import ServerHostError, parse_service_command
from ciao.workspace_access import denial_path, read_denial

SERVER_LABEL = "com.ciao.server"
LEGACY_MENUBAR_LABEL = "com.ciao.menubar"
DEFAULT_PORT = 8443
MIGRATION_RECEIPT = "desktop-migration.json"
DESKTOP_BUNDLE_ID = "local.ciaobot.app"
DESKTOP_EXECUTABLE = "ciaobot-desktop"
LEGACY_EXECUTABLE = "CiaobotServer"

Runner = Callable[..., subprocess.CompletedProcess[str]]

# A reload boots the job out, waits for launchd to forget it, then bootstraps
# the edited plist. The hosted job takes up to ~35 s to stop (its shutdown
# grace), so the wait outlasts that; bootstrapping while the old job still
# exists fails or leaves a half-dead service.
RELOAD_TEARDOWN_TIMEOUT_S = 45
RELOAD_POLL_INTERVAL_S = 0.5
# How long a start waits for the engine to come up, or to record that it
# could not bind its port. A start that reports "started" before the engine
# has had a chance to fail is a start that reports a hope. The engine
# reaches its bind within a second or two of starting, so this is about
# the failure and not about the engine being ready.
# How long `ciao service start` waits for the engine it started to answer as
# itself before calling the start unconfirmed (#1938). Measured on 2026-10-23:
# a cold `python -m ciao run` from a checkout served /api/startup-status after
# 4.1 s and was fully ready at 4.7 s on an Apple Silicon Mac, so the previous
# 2 s deadline expired before every engine had bound its port. A launchd-spawned
# install pays for a fresh interpreter and the first import on top of that;
# the installer script allows 60 s for the same wait. 30 s leaves room for a
# slow disk without making a dead start wait a minute to be reported.
START_SETTLE_TIMEOUT_S = 30.0
# One HTTP probe never waits longer than this, so the deadline above is what
# bounds the whole wait, not the sum of stalled probes.
START_PROBE_TIMEOUT_S = 2.0
START_POLL_INTERVAL_S = 0.5
# How long one `launchctl print` probe may take. It answers from launchd's
# in-memory registry; a launchd that does not answer is uncertainty, not
# absence, and must not be waited on forever.
JOB_PROBE_TIMEOUT_S = 10.0
# How long one fresh `launchctl bootstrap` may take. It registers a job; it
# never waits for the engine to come up (setup's RunAtLoad starts that, and the
# caller verifies readiness separately), so this bounds a stuck launchd only.
FRESH_ACTIVATION_TIMEOUT_S = 20.0
# The system launchctl by absolute path. The new native activation paths use
# this so a caller-controlled PATH cannot substitute another program; the
# legacy load/reload path below is unchanged and still invokes ``launchctl`` by
# name.
LAUNCHCTL = "/bin/launchctl"
SERVER_LOCK_NAME = "server.lock"
# Test seam: the teardown poll sleeps through this, so tests run without waiting.
_sleep: Callable[[float], None] = time.sleep


def _getuid() -> int:
    """This user's uid, the ``<uid>`` in launchd's ``gui/<uid>`` domain.

    ``os.getuid`` does not exist on Windows, where this module is imported (its
    ``ServiceResult`` and ``read_dotenv`` are shared) but no launchd path runs.
    Read through ``getattr`` so mypy's win32 pass type-checks it too, and typed
    locally because the ``getattr`` is ``Any`` (``no-any-return``).
    """
    getuid: Callable[[], int] = getattr(os, "getuid")
    return getuid()


def default_launch_agents_dir() -> Path:
    """Where LaunchAgent plists are written, honouring ``CIAO_LAUNCH_AGENTS_DIR``.

    Defaults to the user's real ``~/Library/LaunchAgents``. The override exists
    because the default is the operator's live install: `setup_workspace` writes
    `com.ciao.server.plist` there, so any test calling it repointed the real
    agent at a pytest tmpdir. The suite stayed green while the engine was
    relaunched against a temp workspace — and indexed that fixture vault over
    the operator's own search database.

    Same shape and same reason as ``CIAO_MEMORY_DIR`` in `fts_search`: the thing
    that failed was remembering to pass the argument, so the default itself is
    what has to be redirectable.
    """
    override = os.environ.get("CIAO_LAUNCH_AGENTS_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / "Library" / "LaunchAgents"


def live_launch_agents_dir() -> Path:
    """The real per-user LaunchAgents dir, ignoring ``CIAO_LAUNCH_AGENTS_DIR``."""
    return Path.home() / "Library" / "LaunchAgents"


def bootout_agent(label: str) -> None:
    """Best-effort ``launchctl bootout gui/<uid>/<label>``; output is discarded."""
    try:
        subprocess.run(
            ["launchctl", "bootout", f"gui/{_getuid()}/{label}"],
            check=False,
            capture_output=True,
        )
    except OSError:
        pass


def load_agent(definition: Path) -> int:
    """``launchctl unload`` (a quiet probe) then ``launchctl load -w``; return load's status."""
    # The unload is a probe: during an install the agent is normally
    # not loaded, and launchctl says so on stderr ("Unload failed: 5:
    # Input/output error"). check=False swallows the status but not the
    # output, and install.sh redirects only stdout - so that expected
    # non-event was the first line a user saw when re-running the
    # installer over a configured workspace, ahead of the success lines.
    subprocess.run(
        ["launchctl", "unload", str(definition)],
        check=False,
        stderr=subprocess.DEVNULL,
    )
    return subprocess.run(
        ["launchctl", "load", "-w", str(definition)],
        check=False,
    ).returncode


def _definition_digest(definition: Path) -> str:
    """The sha256 of exactly the regular file at ``definition``.

    The read is :func:`ciao.os_support.definition.read_definition`, shared with
    the native handoff so the two digests cannot drift: it is binary where the
    platform has ``O_BINARY``, rejects a size that the read did not cover, and
    refuses a missing, special, replaced or changed file.
    """
    return read_definition(definition, max_bytes=MAX_DEFINITION_BYTES).sha256


def _require_definition_digest(definition: Path, expected_sha256: str) -> None:
    if _definition_digest(definition) != expected_sha256:
        raise RuntimeError(
            f"the service definition changed after setup recorded it: {definition}"
        )


def load_fresh_agent(definition: Path, *, expected_sha256: str) -> int:
    """Bootstrap the server job from exactly the definition the handoff recorded.

    Fresh activation only. The job must be *proven* absent in this user's GUI
    domain through :func:`server_job_absent` (the qualified
    ``launchctl print gui/<uid>/<label>`` probe; ``launchctl list`` is
    unqualified and its silence is not absence), so this never unloads, boots
    out, reloads or kickstarts a job that is already registered — an
    intervening job is a refusal, never something to replace. The definition
    is digested before the first probe and again after all its probes,
    immediately before submission, so changed bytes, a symlink, a FIFO or an
    inode/mtime race refuse without a slow probe widening the gap between the
    last check and launchd's open. A failed, unqualified or other-domain probe
    answer, a nonzero bootstrap and a timeout refuse as well.

    A launchd override that disables the label still answers "not found" to
    the probe, so absence alone is not freshness. The read-only
    ``launchctl print-disabled gui/<uid>`` listing is consulted before the
    bootstrap: an explicit disabled label is existing service configuration, not
    a fresh install, and clearing it is the operator's decision. This function
    therefore never calls ``launchctl enable``; it refuses and names the action
    for the operator. That is a deliberate limit of fresh-only activation, not a
    transient failure to retry.

    Atomicity limit: the digest check and launchd opening the pathname are two
    separate steps. A same-UID writer can replace the bytes between the last
    check and launchd's open, and launchd has no compare-and-bootstrap call, so
    the check narrows that window and does not close it. Bootstrap-only
    semantics are what keep a *concurrently registered* job from being
    unloaded or replaced.

    A success does not kickstart: setup's ``RunAtLoad`` starts the fresh job,
    and the caller verifies the engine separately. Returns 0 on success.

    Version note: the accepted absence answer and the ``print-disabled`` shape
    were recorded from read-only probes on one current macOS. The readers pin
    exactly what those probes printed and refuse everything else, but they have
    not been validated against every macOS release. #2340 must exercise them on
    the supported macOS range before native activation is wired in; until then
    an unfamiliar answer is a refusal, never a guessed absence.
    """
    if re.fullmatch(r"[0-9a-f]{64}", expected_sha256) is None:
        raise ValueError(
            "the expected service definition digest is not a lowercase sha256"
        )
    _require_definition_digest(definition, expected_sha256)
    if not server_job_absent():
        raise RuntimeError(f"{SERVER_LABEL} is already registered with launchd")
    if launchd_override_disabled():
        override = f"gui/{_getuid()}/{SERVER_LABEL}"
        raise RuntimeError(
            f"{SERVER_LABEL} is disabled in gui/{_getuid()} and fresh activation "
            "will not change that override. Enable it yourself if that is what "
            f"you want: launchctl enable {override}"
        )
    # Last, after all the probes: the override query may take ten seconds, so
    # hashing before it would leave that whole window open before launchd
    # opens the pathname. This check is the closest one to that open.
    _require_definition_digest(definition, expected_sha256)
    try:
        completed = subprocess.run(
            [LAUNCHCTL, "bootstrap", f"gui/{_getuid()}", str(definition)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=FRESH_ACTIVATION_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not bootstrap {SERVER_LABEL}: {exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        if re.search(r"service is disabled\b", detail, re.IGNORECASE):
            # A disabled override in the user's domain is existing service
            # configuration. Fresh activation must not clear it with
            # `launchctl enable` — that is the operator's decision — so it
            # fails closed with the exact action they can take.
            raise RuntimeError(
                f"launchctl bootstrap failed: {detail}; {SERVER_LABEL} is "
                f"disabled in gui/{_getuid()} and fresh activation will not "
                "change that override. Enable it yourself if that is what you "
                f"want: launchctl enable gui/{_getuid()}/{SERVER_LABEL}"
            )
        raise RuntimeError(
            f"launchctl bootstrap failed: {detail}"
            if detail
            else f"launchctl bootstrap failed for {SERVER_LABEL}"
        )
    return 0


def schedule_server_handoff() -> bool:
    """Spawn a detached helper that loads and kickstarts the server agent.

    Returns False when the live plist is missing or the spawn fails.
    """
    plist = live_launch_agents_dir() / f"{SERVER_LABEL}.plist"
    if not plist.exists():
        return False
    script = (
        "sleep 3; "
        f"/bin/launchctl load -w '{plist}' 2>/dev/null; "
        f"/bin/launchctl kickstart gui/{_getuid()}/{SERVER_LABEL} 2>/dev/null; "
        "exit 0"
    )
    try:
        subprocess.Popen(
            ["/bin/sh", "-c", script],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return True


@dataclass(frozen=True, slots=True)
class DesktopRuntime:
    workspace: str
    runtime_root: str
    port: int
    server_plist: str
    python_path: str


@dataclass(frozen=True, slots=True)
class ServiceResult:
    ok: bool
    action: str
    message: str
    details: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def read_dotenv(path: Path) -> dict[str, str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return parse_dotenv(text)


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse ``KEY=value`` lines; the caller decides how the file was read."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def hosted_service_python(arguments: object) -> str | None:
    """The interpreter a hosted ``ProgramArguments`` serves, or ``None``.

    ``None`` for anything that is not the strict hosted shape
    (``Ciaobot Server serve --python <interpreter>``) as
    :func:`ciao.server_host.parse_service_command` reads it, including every
    malformed value the parser refuses. Syntax only: it never proves ownership.
    """
    try:
        parsed = parse_service_command(arguments)
    except ServerHostError:
        return None
    return parsed.python if parsed.mode == "hosted" else None


def host_interpreter_for(engine: str) -> str:
    """The interpreter a verified host must serve, from the setup program.

    ``ciao.cli._host_engine_interpreter`` applies exactly this rule when it
    renders a hosted definition: a host cannot serve the ``ciao`` console —
    its own parser demands a python executable — so setup renders this
    process's interpreter for that one documented console name and passes
    anything else through, where the host renderer refuses it with the reason.
    This function is the same rule for the native handoff, which records the
    interpreter the hosted definition actually serves.

    The direct shape does not use this mapping. A direct console definition
    runs the console script itself, and the kernel then runs the interpreter
    named on its first line; :func:`ciao.native_handoff.resolve_console_interpreter`
    reads that interpreter so the result can identify both files.
    """
    if Path(engine).name.lower() == "ciao":
        return sys.executable
    return engine


def service_python_path(arguments: object) -> str:
    """The interpreter the service definition actually runs.

    A **hosted** definition (``Ciaobot Server serve --python <interpreter>``)
    runs the native host, which is not a Python interpreter: reporting its
    executable as ``python_path`` would make ``update_engine``'s bundled-engine
    check and every consumer that hands this value to a process start the wrong
    thing. The hosted shape is recognised through
    :func:`ciao.server_host.parse_service_command` and the answer is the
    interpreter the host was told to serve.

    Anything else — including a legacy direct argv the parser refuses, an
    arbitrary console name or a case variant — keeps today's behavior: argv[0]
    is the program, no refusal. Recognition stays permissive on purpose;
    refusing here would turn a working install's status and classification into
    an error, which is exactly the operator state this must not drop. A missing
    or non-string argv[0] names no program, so the answer is this interpreter.
    """
    hosted = hosted_service_python(arguments)
    if hosted is not None:
        return hosted
    if (
        isinstance(arguments, (list, tuple))
        and arguments
        and isinstance(arguments[0], str)
    ):
        return arguments[0]
    return sys.executable


def parse_launchctl_print_arguments(text: str) -> list[str] | None:
    """The argv a loaded job runs, from ``launchctl print gui/<uid>/<label>``.

    Reads the ``arguments = {`` block (one element per line, closed by ``}``) as
    launchd renders it. ``None`` when there is no such block: the job is not
    loaded (launchctl prints nothing usable for it), or the output is not what
    launchd prints. Pure: it never runs launchctl.
    """
    if not launchctl_print_field(text, "arguments"):
        return None
    return launchctl_print_list(text, "arguments")


def parse_launchctl_last_exit_code(text: str) -> int | None:
    """The code the loaded job last exited with, or ``None`` when it has not.

    launchd renders it as ``last exit code = 3`` inside the job's own block.
    A job that has never run has no such line, and a launchctl that printed
    nothing usable for the job has none either: both answer ``None``, which
    is "the machine did not say", not "the job exited cleanly".
    """
    value = launchctl_print_field(text, "last exit code").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def load_server_plist(launch_agents_dir: Path | None = None) -> dict[str, Any] | None:
    """The parsed ``com.ciao.server.plist``, or ``None`` when missing or unreadable.

    The one reader of the server LaunchAgent. ``launch_agents_dir`` defaults
    to :func:`default_launch_agents_dir`. A missing, unreadable, malformed or
    half-written plist (``ExpatError`` is not a ``ValueError``) and one whose
    root is not a dict all answer ``None``.
    """
    agents = (
        default_launch_agents_dir()
        if launch_agents_dir is None
        else Path(launch_agents_dir).expanduser()
    )
    try:
        with (agents / f"{SERVER_LABEL}.plist").open("rb") as handle:
            data: Any = plistlib.load(handle)
    except (OSError, ValueError, ExpatError):
        return None
    return data if isinstance(data, dict) else None


def _plist_program_arguments(plist: dict[str, Any] | None) -> list[str] | None:
    """The ``ProgramArguments`` of a parsed LaunchAgent plist, or ``None``."""
    arguments = plist.get("ProgramArguments") if plist is not None else None
    if (
        not isinstance(arguments, list)
        or not arguments
        or not all(isinstance(item, str) for item in arguments)
    ):
        return None
    return list(arguments)


def _loaded_definition(uid: int, runner: Runner) -> tuple[bool, list[str] | None]:
    """Whether launchd holds the server job, and the argv it runs.

    ``(False, None)`` when the job is not loaded or launchctl cannot be run.
    ``(True, None)`` when it is loaded but its arguments are not readable.
    """
    loaded, arguments, _code = _loaded_job(uid, runner)
    return loaded, arguments


_LAUNCHCTL_LIST_HEADER = ("pid", "status", "label")


_SIGNED_STATUS = re.compile(r"-?\d+")


def parse_launchctl_list(stdout: str) -> set[str]:
    """Labels from ``launchctl list``; unrecognised output is an error.

    ``launchctl list`` always prints the ``PID Status Label`` header, even with
    no jobs loaded, so an empty or differently shaped response is not evidence
    that nothing is registered. Rows are ``PID``, exit status (both ``-`` or a
    number, and the status is negative when the job was killed by a signal) and
    the label.
    """
    lines = [line for line in stdout.splitlines() if line.strip()]
    if not lines or tuple(field.casefold() for field in lines[0].split()) != _LAUNCHCTL_LIST_HEADER:
        raise RuntimeError("launchctl list returned unrecognised output")
    labels: set[str] = set()
    for line in lines[1:]:
        fields = line.split()
        if (
            len(fields) != 3
            or not (fields[0] == "-" or fields[0].isdigit())
            or not (fields[1] == "-" or _SIGNED_STATUS.fullmatch(fields[1]))
        ):
            raise RuntimeError(f"launchctl list returned a malformed row: {line!r}")
        labels.add(fields[2])
    return labels


def server_job_registered() -> bool:
    """Strictly query launchd for the engine label; uncertainty is an error."""
    try:
        completed = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", check=False, timeout=10.0,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"Could not query launchd: {exc}") from exc
    if completed.returncode != 0:
        raise RuntimeError(f"launchctl list failed: {(completed.stderr or '').strip()}")
    return SERVER_LABEL in parse_launchctl_list(completed.stdout or "")


#: launchd's one recognized "this job is not here" answer. It names the job and
#: the domain; anything else — a different label, a different or prefixed uid,
#: a generic failure — is uncertainty, not absence.
_ABSENT_JOB_LINE = re.compile(
    r'Could not find service "(?P<label>[^"]+)" in domain for user gui: '
    r"(?P<uid>\d+)"
)

#: The status launchd pairs with the absence line. A different status is a
#: different fact even when the text looks right.
_ABSENT_JOB_STATUS = 113

#: The preamble a current macOS launchctl writes before the absence line. It is
#: part of the exact answer, not tolerated noise: recorded from a read-only
#: probe of a label that does not exist (`Bad request.` then the line, exit
#: 113). An older launchctl writes the line alone.
_ABSENT_JOB_PREFIXES = ("Bad request.\n", "")


def _confirmed_absent_answer(
    completed: subprocess.CompletedProcess[str], *, label: str, uid: int
) -> bool:
    """Whether this is exactly launchd's absence answer for label+uid.

    The known answer is the absence line, optionally preceded by exactly one
    ``Bad request.`` line, on stderr, with empty stdout and exit status 113.
    A foreign label, a neighbouring or prefixed uid, an extra line, output on
    the other stream and any other status all refuse.
    """
    if completed.returncode != _ABSENT_JOB_STATUS or (completed.stdout or "").strip():
        return False
    text = (completed.stderr or "").strip()
    for prefix in _ABSENT_JOB_PREFIXES:
        if not text.startswith(prefix):
            continue
        match = _ABSENT_JOB_LINE.fullmatch(text[len(prefix) :])
        if (
            match is not None
            and match.group("label") == label
            and int(match.group("uid")) == uid
        ):
            return True
    return False


def server_job_absent(*, uid: int | None = None) -> bool:
    """Whether launchd proves the server job is absent from this user's domain.

    ``launchctl print gui/<uid>/<label>`` asks the qualified question that
    ``launchctl bootstrap gui/<uid>`` acts on. ``True`` is returned only for
    launchd's exact absence answer for *this* label and *this* domain at its
    known status 113: a current launchctl writes ``Bad request.`` then
    ``Could not find service "<label>" in domain for user gui: <uid>``, an older
    one the line alone. A job dump answers ``False`` (the job is there). Any
    other status, a wrong label, a prefixed uid (``gui: 5010`` for 501), another
    domain, extra or unrelated output, a failed query or a timeout raises:
    uncertainty is not absence.
    """
    resolved_uid = _getuid() if uid is None else uid
    try:
        completed = subprocess.run(
            [LAUNCHCTL, "print", f"gui/{resolved_uid}/{SERVER_LABEL}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=JOB_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not probe {SERVER_LABEL} in gui/{resolved_uid}: {exc}"
        ) from exc
    if completed.returncode == 0:
        return False
    if _confirmed_absent_answer(completed, label=SERVER_LABEL, uid=resolved_uid):
        return True
    detail = ((completed.stderr or "") + (completed.stdout or "")).strip()
    raise RuntimeError(
        f"launchd did not confirm {SERVER_LABEL} absent from gui/{resolved_uid}"
        + (f": {detail}" if detail else "")
    )


def launchd_override_disabled(*, uid: int | None = None) -> bool:
    """Whether launchd's user-domain override explicitly disables the job.

    ``launchctl print-disabled gui/<uid>`` is the only query for the per-label
    override, and it is read-only. A readable listing that does not name
    :data:`SERVER_LABEL` answers ``False``; an explicit ``disabled``/``true``
    entry answers ``True``. A failed query, a timeout, malformed output or a
    label listed more than once raises: uncertainty is not an answer, and a
    caller deciding whether a job may be started must not guess.

    This function never enables, disables, loads, unloads or otherwise changes
    the override; it only reads it.
    """
    resolved_uid = _getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    try:
        completed = subprocess.run(
            [LAUNCHCTL, "print-disabled", domain],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=JOB_PROBE_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(
            f"Could not read launchd overrides for {domain}: {exc}"
        ) from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(
            f"launchctl print-disabled {domain} failed"
            + (f": {detail}" if detail else "")
        )
    # Local import: service_login owns the one reader for the real output shape
    # and imports this module at module level.
    from ciao.service_login import strict_disabled_override

    state = strict_disabled_override(completed.stdout or "", SERVER_LABEL)
    if state is None:
        raise RuntimeError(
            f"launchctl print-disabled {domain} returned output that cannot be read"
        )
    return state


def _loaded_job(
    uid: int, runner: Runner
) -> tuple[bool, list[str] | None, int | None]:
    """What launchd holds for the server job: loaded, its argv, its exit code.

    One ``launchctl print`` answers all three, so the job is asked once: a
    second call could see a different job (one that just exited, one that
    was just reloaded) and the three facts would no longer be the same
    moment.
    """
    try:
        completed = _launchctl(["print", f"gui/{uid}/{SERVER_LABEL}"], runner=runner)
    except OSError:
        return False, None, None
    printed = completed.stdout or ""
    if completed.returncode != 0:
        return False, None, None
    return (
        True,
        parse_launchctl_print_arguments(printed),
        parse_launchctl_last_exit_code(printed),
    )


def server_last_exit_code(*, uid: int | None = None, runner: Runner = subprocess.run) -> int | None:
    """Read launchd's last observed server exit code without changing service state."""
    _loaded, _arguments, exit_code = _loaded_job(_getuid() if uid is None else uid, runner)
    return exit_code


def _plist_matches_loaded(
    loaded_arguments: list[str] | None,
    plist_arguments: list[str] | None,
) -> bool | None:
    """Whether the plist on disk matches the loaded job; ``None`` if either is unknown."""
    if not loaded_arguments or not plist_arguments:
        return None
    return loaded_arguments == plist_arguments


def launchctl_print_field(printed: str, key: str) -> str:
    """The text ``launchctl print`` rendered for ``key``, or ``""``.

    launchd has printed a job's values in three shapes across versions: a bare
    scalar on the key's own line, a list opened on that same line, and a list
    whose opening bracket is on the line *after* the key. A list of any shape is
    flattened to its own lines here, so the callers only decide what a token in
    it means, and a key launchd did not render at all answers ``""`` — which is
    evidence of nothing, and so never refuses an update and never stands a
    recovery down.
    """
    lines = printed.splitlines()
    for index, line in enumerate(lines):
        head, separator, rest = line.partition("=")
        if not separator or head.strip() != key:
            continue
        value = rest.strip()
        if value[:1] not in {"(", "{"}:
            return value
        parts = [value]
        # The list's own delimiters, so a block that opens here is collected up
        # to the line that closes it — one argument per line, in every shape.
        depth = value.count("(") + value.count("{") - value.count(")") - value.count("}")
        if depth <= 0:
            # Opened and closed on the key's own line: the value is that line,
            # not that line plus whatever key launchd printed after it.
            return value
        for nxt in lines[index + 1 :]:
            parts.append(nxt)
            depth += nxt.count("(") + nxt.count("{") - nxt.count(")") - nxt.count("}")
            if depth <= 0:
                break
        return "\n".join(parts)
    return ""


def launchctl_print_tokens(printed: str, key: str) -> list[str]:
    """The tokens in a value ``launchctl print`` rendered for ``key``."""
    return [
        token
        for line in launchctl_print_field(printed, key).splitlines()
        for token in re.findall(r"[^\s,(){}]+", line)
    ]


def launchctl_print_list(printed: str, key: str) -> list[str]:
    """The elements of a list ``launchctl print`` rendered for ``key``.

    launchd renders a job's list one element per line inside a delimited block,
    and that layout is what preserves an element that contains a space — the
    native host's own path does (``Ciaobot Server.app``), and so may the
    interpreter it serves. Splitting those lines on whitespace would truncate the
    host path at the first space, so a multi-line block is read line by line,
    including any element on the line that opens it. The older single-line
    shapes (``( -I -m ... )`` or ``{ -I -m ... }``) carry no spaces in their
    tokens and are read as whitespace-separated tokens, as :func:`launchctl_print_tokens`
    does. Quotes are stripped from every element, exactly as from a scalar
    program, so the two agree when :func:`_loaded_server_command` folds them.
    """
    lines = [line.strip() for line in launchctl_print_field(printed, key).splitlines()]
    if not lines:
        return []
    if len(lines) == 1:
        elements = re.findall(r"[^\s,(){}]+", lines[0])
    else:
        # Only the block's own delimiters come off: the opening one on the first
        # line and the closing one on the last. An element may itself end in a
        # bracket (`/tools/env (copy)`), so no line is stripped of them wholesale.
        lines[0] = lines[0][1:]
        if lines[-1][-1:] in {")", "}"}:
            lines[-1] = lines[-1][:-1]
        elements = [line.strip().rstrip(",").strip() for line in lines]
    return [stripped for element in elements if (stripped := element.strip("\"'"))]


def discover_runtime(
    *,
    launch_agents_dir: Path | None = None,
    environ: dict[str, str] | None = None,
) -> DesktopRuntime:
    """Resolve workspace, port, and runtime root using the desktop contract."""

    env = os.environ if environ is None else environ
    agents = (
        default_launch_agents_dir()
        if launch_agents_dir is None
        else Path(launch_agents_dir).expanduser()
    )
    plist_path = agents / f"{SERVER_LABEL}.plist"
    plist: dict[str, Any] = load_server_plist(agents) or {}

    plist_env = plist.get("EnvironmentVariables")
    if not isinstance(plist_env, dict):
        plist_env = {}
    workspace_raw = str(
        plist_env.get("CIAO_WORKSPACE")
        or plist.get("WorkingDirectory")
        or env.get("CIAO_WORKSPACE")
        or ""
    ).strip()
    workspace = Path(workspace_raw).expanduser().resolve() if workspace_raw else None
    dotenv = read_dotenv(workspace / ".env") if workspace else {}

    raw_port = str(
        dotenv.get("PWA_PORT")
        or plist_env.get("CIAO_PORT")
        or env.get("CIAO_PORT")
        or DEFAULT_PORT
    )
    try:
        port = int(raw_port)
    except ValueError:
        port = DEFAULT_PORT
    if not 1 <= port <= 65535:
        port = DEFAULT_PORT

    # The service definition is the only place a runtime root is set; setup
    # always renders `<workspace>/.runtime` into it.
    runtime_raw = str(plist_env.get("CIAO_RUNTIME_ROOT") or "").strip()
    if runtime_raw:
        runtime = Path(runtime_raw).expanduser()
        if not runtime.is_absolute() and workspace:
            runtime = workspace / runtime
    elif workspace:
        runtime = workspace / ".runtime"
    else:
        runtime = Path(".runtime").resolve()

    arguments = plist.get("ProgramArguments")
    python_path = service_python_path(arguments)
    return DesktopRuntime(
        workspace=str(workspace or ""),
        runtime_root=str(runtime.resolve()),
        port=port,
        server_plist=str(plist_path),
        python_path=python_path,
    )


def _launchctl(
    args: Sequence[str],
    *,
    runner: Runner = subprocess.run,
) -> subprocess.CompletedProcess[str]:
    return runner(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _command_error(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stderr or completed.stdout or "").strip()


class RestartBlockersUnavailable(RuntimeError):
    """The engine answered but its restart blockers could not be read."""


def restart_blockers(port: int, *, timeout: float = 2.0) -> list[dict[str, Any]]:
    """Chats a restart would interrupt, from the engine's loopback-only route.

    An engine that is not listening has nothing to interrupt, so it reads as
    empty. Any other failure raises, so the gate refuses rather than guesses.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/admin/restart-blockers",
            timeout=timeout,
        ) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RestartBlockersUnavailable(f"the engine answered HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ConnectionRefusedError):
            return []
        raise RestartBlockersUnavailable(str(exc)) from exc
    except ConnectionRefusedError:
        return []
    except OSError as exc:
        raise RestartBlockersUnavailable(str(exc)) from exc
    except ValueError as exc:
        raise RestartBlockersUnavailable("invalid JSON from the engine") from exc
    values = payload.get("restart_blockers") if isinstance(payload, dict) else None
    if not isinstance(values, list):
        raise RestartBlockersUnavailable("unexpected answer from the engine")
    return [
        {
            "chat_id": str(value["chat_id"]),
            "reasons": [str(reason) for reason in value.get("reasons") or []],
        }
        for value in values
        if isinstance(value, dict) and value.get("chat_id")
    ]


_GATE_VERBS = {
    "stop": "stopping the engine",
    "restart": "restarting the engine",
    "update-engine": "updating Ciaobot",
}


def restart_gate(
    port: int,
    action: str,
    *,
    force: bool,
    details: dict[str, Any] | None = None,
) -> ServiceResult | None:
    """Refuse a stop/restart/update that would interrupt chats, or cannot tell."""
    if force:
        return None
    verb = _GATE_VERBS[action]
    try:
        blockers = restart_blockers(port)
    except RestartBlockersUnavailable as exc:
        # An engine that is not serving has nothing to interrupt. Some hosts
        # (Windows runners among them) time out instead of refusing a closed
        # port, so check before failing closed on an engine that is down.
        if not server_reachable(port):
            return None
        return ServiceResult(
            False,
            action,
            f"Could not read which chats {verb} would interrupt ({exc}). Pass --force to continue anyway.",
            {**(details or {}), "restart_blockers": None, "requires_confirmation": True},
        )
    if not blockers:
        return None
    listed = ", ".join(f"{b['chat_id']} ({', '.join(b['reasons'])})" for b in blockers)
    return ServiceResult(
        False,
        action,
        f"Chats that would be interrupted must be confirmed before {verb}: {listed}. "
        "Let them finish or answer their open cards, or pass --force.",
        {**(details or {}), "restart_blockers": blockers, "requires_confirmation": True},
    )


def server_reachable(port: int, *, timeout: float = 2.0) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/startup-status",
            timeout=timeout,
        ) as response:
            return 200 <= int(response.status) < 300
    except (OSError, urllib.error.URLError, ValueError):
        return False


@dataclass(frozen=True, slots=True)
class StartOutcome:
    """What `wait_for_engine` could establish about the engine it waited for.

    ``state`` is one of ``started`` (the new engine answered as itself),
    ``port_in_use`` (it recorded that it could not bind), ``denied`` (macOS
    refused it the workspace) or ``timeout`` (nothing could be confirmed
    before the deadline). A timeout is not a start: it is the absence of
    evidence, and the caller reports it as a failure (#1938).
    """

    state: str
    message: str
    startup_id: str | None = None
    pid: int | None = None

    @property
    def ok(self) -> bool:
        return self.state == "started"


def read_lock_owner(runtime: DesktopRuntime) -> dict[str, Any] | None:
    """The metadata the engine wrote into its runtime lock, or ``None``.

    The engine writes ``server.lock`` under its runtime directory when it
    takes the lock (:class:`ciao.instance_lock.WorkspaceInstanceLock`): pid,
    port, workspace, runtime, status and a ``startup_id`` fresh for every run.
    The file is advisory and diagnostic: a missing, unreadable or malformed
    one reads as "no owner known", never as an error.
    """
    path = Path(runtime.runtime_root) / SERVER_LOCK_NAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None
    return value if isinstance(value, dict) else None


def engine_identity(port: int, *, timeout: float = START_PROBE_TIMEOUT_S) -> dict[str, Any] | None:
    """The startup-status payload of whatever answers ``port``, if it is an engine.

    ``None`` unless the answer is a JSON object carrying a ``startup_id``: an
    unrelated listener on the port, a bare 2xx, or an engine too old to
    report an identity all count as "no engine identified". A 2xx alone
    satisfied the old wait, which is how an unrelated service could make a
    failed start read as a success.
    """
    try:
        with urllib.request.urlopen(
            f"http://localhost:{port}/api/startup-status",
            timeout=timeout,
        ) as response:
            if not 200 <= int(response.status) < 300:
                return None
            payload = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    startup_id = payload.get("startup_id")
    if not isinstance(startup_id, str) or not startup_id:
        return None
    return payload


def _fresh_lock_owner(
    runtime: DesktopRuntime,
    previous_startup_id: str | None,
) -> dict[str, Any] | None:
    """The lock owner, when it is a new run of *this* install's engine.

    Fresh means: status ``running``, a ``startup_id`` that is not the one the
    previous run had, and the port and runtime directory this install is
    configured with. Another Ciaobot runtime that happens to be running, or
    the run that was there before the kickstart, is not evidence that the
    engine just started.
    """
    owner = read_lock_owner(runtime)
    if not owner or owner.get("status") != "running":
        return None
    startup_id = owner.get("startup_id")
    if not isinstance(startup_id, str) or not startup_id or startup_id == previous_startup_id:
        return None
    if owner.get("port") != runtime.port:
        return None
    try:
        same_runtime = Path(str(owner.get("runtime", ""))).resolve() == Path(runtime.runtime_root).resolve()
    except OSError:
        same_runtime = False
    if not same_runtime:
        return None
    return owner


def _denial_recorded_since(started_at: float) -> str:
    """A workspace-access denial the engine recorded after ``started_at``.

    The denial record is per user and the supervisor only clears it once it
    has probed the workspace again, so for the first seconds of a start the
    file may still carry an earlier run's verdict. An old one must not fail a
    start that is still booting: only a record written since the kickstart
    counts.
    """
    path = denial_path()
    try:
        if path.stat().st_mtime < started_at - 1.0:
            return ""
    except OSError:
        return ""
    return read_denial(path)


def wait_for_engine(
    runtime: DesktopRuntime,
    *,
    previous: dict[str, Any] | None = None,
    started_at: float | None = None,
    timeout: float = START_SETTLE_TIMEOUT_S,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> StartOutcome:
    """Wait for the engine a kickstart just started to answer as itself.

    ``previous`` is the lock owner read *before* the kickstart (see
    :func:`read_lock_owner`), so the run that was there before cannot pass
    for the new one. The start is confirmed only when the lock names a fresh
    run of this install (:func:`_fresh_lock_owner`) *and* the engine on the
    port reports that same ``startup_id`` (:func:`engine_identity`).

    Bounded by ``timeout``: the engine is starting in the background, and a
    start that waits forever is a start that never answers. A bind failure
    or a workspace-access denial recorded during the wait ends it at once.
    Reaching the deadline is reported as a timeout, never as a start.
    ``clock`` and ``sleep`` are injectable so a test can run the whole wait
    without wall-clock time.
    """
    previous_startup_id = previous.get("startup_id") if previous else None
    if not isinstance(previous_startup_id, str):
        previous_startup_id = None
    since = time.time() if started_at is None else started_at
    deadline = clock() + timeout
    while True:
        reason = read_port_in_use()
        if reason:
            return StartOutcome("port_in_use", reason)
        denied = _denial_recorded_since(since)
        if denied:
            return StartOutcome("denied", denied)
        owner = _fresh_lock_owner(runtime, previous_startup_id)
        if owner is not None:
            remaining = deadline - clock()
            probe = max(0.1, min(START_PROBE_TIMEOUT_S, remaining))
            payload = engine_identity(runtime.port, timeout=probe)
            if payload is not None and payload.get("startup_id") == owner["startup_id"]:
                pid = payload.get("pid")
                return StartOutcome(
                    "started",
                    "Ciaobot engine started.",
                    startup_id=str(owner["startup_id"]),
                    pid=pid if isinstance(pid, int) and not isinstance(pid, bool) else None,
                )
        if clock() >= deadline:
            return StartOutcome(
                "timeout",
                f"The engine did not confirm its start within {timeout:g}s. It may still be "
                "booting, or it may have exited: run `ciao service status`, and read "
                "the engine log if it stays down.",
            )
        sleep(START_POLL_INTERVAL_S)


def port_in_use_reason(
    runtime: DesktopRuntime,
    *,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> str | None:
    """Why the engine could not start, when the port was the reason.

    The engine records the reason where a reader outside it can find it
    (see :mod:`ciao.port_in_use`), because an engine that cannot bind
    cannot serve the endpoint that would report it. The record is the
    signal that matters: it is written by the engine that failed and
    cleared by the next successful bind.

    The launchd job's last exit code is a second, weaker signal. It only
    fires when the engine itself was the process launchd saw exit with the
    port-in-use code - and the supervisor *holds* the job on that code
    instead of exiting, so a held run leaves an exit code of 0 and this
    branch does not fire. It is kept for the case where the engine ran
    without a supervisor, and it is not what a reader should rely on.

    ``None`` when neither says so — the engine may not have run at all, it
    may be running fine, or it may have failed for another reason.
    """
    recorded = read_port_in_use()
    if recorded:
        return recorded
    resolved_uid = _getuid() if uid is None else uid
    _loaded, _arguments, last_exit_code = _loaded_job(resolved_uid, runner)
    if last_exit_code != PORT_IN_USE_EXIT_CODE:
        return None
    return port_in_use_message(runtime.port, Path(runtime.workspace))


def service_status(
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    resolved_uid = _getuid() if uid is None else uid
    installed = Path(runtime.server_plist).is_file()
    loaded, loaded_arguments, last_exit_code = _loaded_job(resolved_uid, runner)
    loaded_python_path = service_python_path(loaded_arguments) if loaded_arguments else None
    plist_matches_loaded = _plist_matches_loaded(
        loaded_arguments,
        _plist_program_arguments(load_server_plist(Path(runtime.server_plist).parent)),
    )
    reachable = server_reachable(runtime.port)
    blockers: list[dict[str, Any]] | None
    try:
        blockers = restart_blockers(runtime.port) if reachable else []
    except RestartBlockersUnavailable:
        blockers = None
    denied = "" if reachable else read_denial()
    port_in_use_reason_text = (
        "" if reachable else port_in_use_reason(runtime, uid=resolved_uid, runner=runner)
    )
    port_in_use = bool(port_in_use_reason_text)
    if reachable:
        message = "Ciaobot engine is running."
    elif denied:
        message = f"Ciaobot engine is not running: {denied}"
    elif port_in_use:
        message = f"Ciaobot engine is not running: {port_in_use_reason_text}"
    else:
        message = "Ciaobot engine is stopped."
    if blockers:
        message += f"\n{len(blockers)} chat(s) would be interrupted by a restart."
    elif blockers is None and reachable:
        message += "\nCould not read restart blockers from the engine."
    if plist_matches_loaded is False:
        message += (
            "\nWarning: the loaded service definition differs from the plist on disk "
            f"(loaded runs {loaded_python_path}, plist runs {runtime.python_path}). "
            "Run `ciao service restart` to reload the edited definition."
        )
    return ServiceResult(
        ok=True,
        action="status",
        message=message,
        details={
            **asdict(runtime),
            "installed": installed,
            "loaded": loaded,
            "loaded_python_path": loaded_python_path,
            "plist_matches_loaded": plist_matches_loaded,
            "reachable": reachable,
            "restart_blockers": blockers,
            "workspace_access_denied": denied or None,
            "last_exit_code": last_exit_code,
            "port_in_use": port_in_use,
            "port_in_use_reason": port_in_use_reason_text or None,
        },
    )


def start_service(
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    """Start the engine, and say why if it could not bind its port.

    The engine records a bind failure where a reader outside it can find it
    (see :mod:`ciao.port_in_use`), because an engine that cannot bind
    cannot serve the endpoint that would report it. The record of an
    earlier failure is cleared first, so what is read back belongs to this
    start and not to a run before it.
    """
    runtime = runtime or discover_runtime()
    plist = Path(runtime.server_plist)
    if not plist.is_file():
        return ServiceResult(
            False,
            "start",
            "The server LaunchAgent is not installed. Run `ciao service start --workspace <dir>` to register it, or `ciao setup --workspace <dir> --load-launchd`.",
            {**asdict(runtime), "setup_required": True},
        )
    resolved_uid = _getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    clear_port_in_use()
    try:
        _launchctl(["enable", f"{domain}/{SERVER_LABEL}"], runner=runner)
        bootstrap = _launchctl(["bootstrap", domain, str(plist)], runner=runner)
        kickstart = _launchctl(
            ["kickstart", "-k", f"{domain}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "start", str(exc), asdict(runtime))
    if kickstart.returncode != 0:
        detail = _command_error(kickstart) or _command_error(bootstrap)
        return ServiceResult(False, "start", detail or "launchctl start failed", asdict(runtime))
    reason = port_in_use_reason(runtime, uid=resolved_uid, runner=runner)
    if reason is not None:
        return ServiceResult(
            False,
            "start",
            reason,
            {**asdict(runtime), "port_in_use": True},
        )
    return ServiceResult(True, "start", "Ciaobot engine started.", asdict(runtime))


def stop_service(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    refusal = restart_gate(runtime.port, "stop", force=force, details=asdict(runtime))
    if refusal is not None:
        return refusal
    resolved_uid = _getuid() if uid is None else uid
    try:
        completed = _launchctl(
            ["bootout", f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "stop", str(exc), asdict(runtime))
    if completed.returncode != 0 and "could not find service" not in _command_error(completed).lower():
        return ServiceResult(
            False,
            "stop",
            _command_error(completed) or "launchctl stop failed",
            asdict(runtime),
        )
    return ServiceResult(True, "stop", "Ciaobot engine stopped.", asdict(runtime))


def _wait_for_teardown(uid: int, runner: Runner) -> bool:
    """Poll until launchd reports the server job gone; False if it never does."""
    polls = int(RELOAD_TEARDOWN_TIMEOUT_S / RELOAD_POLL_INTERVAL_S)
    for _ in range(polls):
        completed = _launchctl(["print", f"gui/{uid}/{SERVER_LABEL}"], runner=runner)
        text = f"{completed.stdout or ''}\n{completed.stderr or ''}".lower()
        if completed.returncode != 0 and "could not find service" in text:
            return True
        _sleep(RELOAD_POLL_INTERVAL_S)
    return False


def restart_service(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    refusal = restart_gate(runtime.port, "restart", force=force, details=asdict(runtime))
    if refusal is not None:
        return refusal
    resolved_uid = _getuid() if uid is None else uid
    domain = f"gui/{resolved_uid}"
    try:
        loaded, loaded_arguments = _loaded_definition(resolved_uid, runner)
        plist_matches = _plist_matches_loaded(
            loaded_arguments,
            _plist_program_arguments(load_server_plist(Path(runtime.server_plist).parent)),
        )
        if loaded and plist_matches is False:
            if os.environ.get("CIAO_CHAT_ID"):
                # A chat shell runs inside the engine's process tree: booting the
                # job out stops this command along with the engine it reloads.
                return ServiceResult(
                    False,
                    "restart",
                    "Refusing to reload the service definition from inside a Ciaobot chat: "
                    "the reload stops the engine running this command. Run `ciao service restart` "
                    "from a terminal, or use Settings → Restart.",
                    {**asdict(runtime), "restart": "reload", "refused": True},
                )
            # launchd reads the plist only when a job is bootstrapped, so kickstart
            # would keep running the definition it already holds.
            booted_out = _launchctl(["bootout", f"{domain}/{SERVER_LABEL}"], runner=runner)
            if (
                booted_out.returncode != 0
                and "could not find service" not in _command_error(booted_out).lower()
            ):
                return ServiceResult(
                    False,
                    "restart",
                    _command_error(booted_out) or "launchctl bootout failed",
                    {**asdict(runtime), "restart": "reload"},
                )
            if not _wait_for_teardown(resolved_uid, runner):
                # Bootstrapping now would race the old job's shutdown. Leave it to the
                # operator rather than start a second copy against a half-dead one.
                return ServiceResult(
                    False,
                    "restart",
                    f"The engine did not stop within {RELOAD_TEARDOWN_TIMEOUT_S} s, so the edited "
                    "service definition was not loaded. Run `ciao service status`, then retry "
                    "`ciao service restart`.",
                    {**asdict(runtime), "restart": "reload", "teardown_timed_out": True},
                )
            completed = _launchctl(["bootstrap", domain, runtime.server_plist], runner=runner)
            restart_path = "reload"
        else:
            completed = _launchctl(
                ["kickstart", "-k", f"{domain}/{SERVER_LABEL}"],
                runner=runner,
            )
            restart_path = "kickstart"
    except OSError as exc:
        return ServiceResult(False, "restart", str(exc), asdict(runtime))
    details = {**asdict(runtime), "restart": restart_path}
    if completed.returncode != 0:
        return ServiceResult(
            False,
            "restart",
            _command_error(completed) or "launchctl restart failed",
            details,
        )
    message = (
        "Ciaobot engine restarted; reloaded the edited service definition."
        if restart_path == "reload"
        else "Ciaobot engine restarted."
    )
    return ServiceResult(True, "restart", message, details)


def set_login_enabled(
    enabled: bool,
    *,
    runtime: DesktopRuntime | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime()
    if not Path(runtime.server_plist).is_file():
        return ServiceResult(
            False,
            "login",
            "The server LaunchAgent is not installed.",
            {**asdict(runtime), "setup_required": True},
        )
    resolved_uid = _getuid() if uid is None else uid
    verb = "enable" if enabled else "disable"
    try:
        completed = _launchctl(
            [verb, f"gui/{resolved_uid}/{SERVER_LABEL}"],
            runner=runner,
        )
    except OSError as exc:
        return ServiceResult(False, "login", str(exc), asdict(runtime))
    if completed.returncode != 0:
        return ServiceResult(
            False,
            "login",
            _command_error(completed) or f"launchctl {verb} failed",
            asdict(runtime),
        )
    return ServiceResult(
        True,
        "login",
        f"Engine start at login {'enabled' if enabled else 'disabled'}.",
        {**asdict(runtime), "enabled": enabled},
    )


def update_engine(
    *,
    runtime: DesktopRuntime | None = None,
    force: bool = False,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    from ciao.package_version import update_package

    runtime = runtime or discover_runtime()
    refusal = restart_gate(
        runtime.port, "update-engine", force=force, details=asdict(runtime)
    )
    if refusal is not None:
        return refusal
    bundled_engine = bool(
        runtime.python_path
        and "Ciaobot.app/Contents/Resources/ciao-runtime"
        in str(runtime.python_path)
    )
    if bundled_engine:
        return ServiceResult(
            False,
            "update-engine",
            "The bundled engine updates together with Ciaobot.app.",
            {
                **asdict(runtime),
                "already_current": True,
                "bundled": True,
                "command": (
                    "curl -fsSL https://github.com/raffaelefarinaro/ciaobot/"
                    "releases/latest/download/install.sh | sh"
                ),
            },
        )
    result = update_package()
    if not result.get("ok"):
        # A no-op upgrade is not a failure for callers that update the engine
        # and the app together: the app half may still have work to do.
        return ServiceResult(
            False,
            "update-engine",
            str(result.get("error") or "Engine update failed."),
            {
                **asdict(runtime),
                "already_current": bool(result.get("already_current")),
                "update": result,
            },
        )
    restarted = restart_service(runtime=runtime, force=True, runner=runner)
    return ServiceResult(
        restarted.ok,
        "update-engine",
        "Ciaobot engine updated and restarted." if restarted.ok else restarted.message,
        {**asdict(runtime), "update": result},
    )


def _unique_destination(directory: Path, name: str) -> Path:
    candidate = directory / name
    if not candidate.exists():
        return candidate
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return directory / f"{candidate.stem}-{stamp}{candidate.suffix}"


def _bundle_matches(app: Path, *, executable: str) -> bool:
    """Return whether *app* is one of Ciaobot's expected signed bundle shapes."""

    info_path = app / "Contents" / "Info.plist"
    try:
        with info_path.open("rb") as handle:
            info = plistlib.load(handle)
    except (OSError, plistlib.InvalidFileException):
        return False
    return (
        isinstance(info, dict)
        and info.get("CFBundleIdentifier") == DESKTOP_BUNDLE_ID
        and info.get("CFBundleExecutable") == executable
        and (app / "Contents" / "MacOS" / executable).is_file()
    )


def _browser_pwa_paths(running_app: Path | None = None) -> list[str]:
    """Find browser-installed Ciaobot app shims without modifying them."""

    candidates = (
        Path.home() / "Applications" / "Chrome Apps.localized" / "Ciaobot.app",
        Path.home() / "Applications" / "Ciaobot.app",
    )
    running = Path(running_app).resolve() if running_app is not None else None
    return [
        str(candidate)
        for candidate in candidates
        if candidate.is_dir()
        and candidate.resolve() != running
        and not _bundle_matches(candidate, executable=DESKTOP_EXECUTABLE)
    ]


def migrate_legacy_companion(
    *,
    runtime: DesktopRuntime | None = None,
    running_app: Path | None = None,
    launch_agents_dir: Path | None = None,
    applications_dirs: Sequence[Path] | None = None,
    trash_dir: Path | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime(launch_agents_dir=launch_agents_dir)
    if running_app is not None:
        resolved_app = Path(running_app).resolve()
        allowed_roots = tuple(
            Path(root).resolve()
            for root in (
                applications_dirs
                or (Path("/Applications"), Path.home() / "Applications")
            )
        )
        if (
            not _bundle_matches(resolved_app, executable=DESKTOP_EXECUTABLE)
            or resolved_app.name != "Ciaobot.app"
            or resolved_app.parent not in allowed_roots
        ):
            return ServiceResult(
                False,
                "migrate",
                "Migration must be started by the installed Ciaobot.app.",
                {**asdict(runtime), "running_app": str(resolved_app)},
            )
    runtime_root = Path(runtime.runtime_root)
    migration_dir = runtime_root / "migration"
    migration_dir.mkdir(parents=True, exist_ok=True)
    agents = (
        default_launch_agents_dir()
        if launch_agents_dir is None
        else Path(launch_agents_dir)
    )
    old_plist = agents / f"{LEGACY_MENUBAR_LABEL}.plist"
    backup_plist = migration_dir / old_plist.name
    receipt_path = migration_dir / MIGRATION_RECEIPT
    resolved_uid = _getuid() if uid is None else uid
    existing_receipt: dict[str, Any] = {}
    try:
        loaded_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if isinstance(loaded_receipt, dict):
            existing_receipt = loaded_receipt
    except (OSError, ValueError):
        pass
    already_migrated = bool(existing_receipt) and not old_plist.exists()
    moved_app_from = str(existing_receipt.get("legacy_app_original") or "")
    moved_app_to = str(existing_receipt.get("legacy_app_backup") or "")

    try:
        _launchctl(
            ["bootout", f"gui/{resolved_uid}/{LEGACY_MENUBAR_LABEL}"],
            runner=runner,
        )
        _launchctl(
            ["disable", f"gui/{resolved_uid}/{LEGACY_MENUBAR_LABEL}"],
            runner=runner,
        )
        if old_plist.exists():
            if backup_plist.exists():
                return ServiceResult(
                    False,
                    "migrate",
                    "A legacy menubar plist backup already exists.",
                    {**asdict(runtime), "backup": str(backup_plist)},
                )
            shutil.move(str(old_plist), str(backup_plist))

        app_roots = list(applications_dirs or (Path("/Applications"), Path.home() / "Applications"))
        desktop_installed = any(
            _bundle_matches(root / "Ciaobot.app", executable=DESKTOP_EXECUTABLE)
            for root in app_roots
        )
        old_app = next(
            (
                root / "Ciaobot Server.app"
                for root in app_roots
                if _bundle_matches(
                    root / "Ciaobot Server.app",
                    executable=LEGACY_EXECUTABLE,
                )
            ),
            None,
        )
        if (
            not moved_app_to
            and desktop_installed
            and old_app is not None
            and server_reachable(runtime.port)
        ):
            trash = Path.home() / ".Trash" if trash_dir is None else Path(trash_dir)
            trash.mkdir(parents=True, exist_ok=True)
            destination = _unique_destination(trash, old_app.name)
            shutil.move(str(old_app), str(destination))
            moved_app_from = str(old_app)
            moved_app_to = str(destination)

        prefs = runtime_root / "menubar_prefs.json"
        notifications_enabled: bool | None = None
        if prefs.is_file():
            try:
                payload = json.loads(prefs.read_text(encoding="utf-8"))
                value = payload.get("notifications_enabled")
                if isinstance(value, bool):
                    notifications_enabled = value
            except (OSError, ValueError):
                pass
        receipt = {
            "schema_version": 1,
            "migrated_at": existing_receipt.get("migrated_at")
            or datetime.now(UTC).isoformat(),
            "legacy_plist_original": existing_receipt.get("legacy_plist_original")
            or (str(old_plist) if backup_plist.exists() else ""),
            "legacy_plist_backup": existing_receipt.get("legacy_plist_backup")
            or (str(backup_plist) if backup_plist.exists() else ""),
            "legacy_app_original": moved_app_from,
            "legacy_app_backup": moved_app_to,
            "notifications_enabled": (
                existing_receipt.get("notifications_enabled")
                if existing_receipt.get("notifications_enabled") is not None
                else notifications_enabled
            ),
        }
        temporary = receipt_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8", newline="")
        os.replace(temporary, receipt_path)
    except OSError as exc:
        return ServiceResult(False, "migrate", str(exc), asdict(runtime))

    return ServiceResult(
        True,
        "migrate",
        (
            "Legacy companion was already migrated."
            if already_migrated and not moved_app_from
            else "Legacy companion disabled and migration backup recorded."
        ),
        {
            **asdict(runtime),
            "receipt": str(migration_dir / MIGRATION_RECEIPT),
            "already_migrated": already_migrated,
            "browser_pwa_paths": _browser_pwa_paths(running_app),
        },
    )


def rollback_legacy_companion(
    *,
    runtime: DesktopRuntime | None = None,
    launch_agents_dir: Path | None = None,
    uid: int | None = None,
    runner: Runner = subprocess.run,
) -> ServiceResult:
    runtime = runtime or discover_runtime(launch_agents_dir=launch_agents_dir)
    receipt_path = Path(runtime.runtime_root) / "migration" / MIGRATION_RECEIPT
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ServiceResult(
            False,
            "rollback",
            "No valid desktop migration receipt was found.",
            asdict(runtime),
        )
    try:
        original_plist_raw = str(receipt.get("legacy_plist_original") or "")
        backup_plist_raw = str(receipt.get("legacy_plist_backup") or "")
        original_plist = Path(original_plist_raw) if original_plist_raw else None
        backup_plist = Path(backup_plist_raw) if backup_plist_raw else None
        if original_plist is not None and backup_plist is not None and backup_plist.is_file():
            original_plist.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_plist), str(original_plist))

        original_app_raw = str(receipt.get("legacy_app_original") or "")
        backup_app_raw = str(receipt.get("legacy_app_backup") or "")
        original_app = Path(original_app_raw) if original_app_raw else None
        backup_app = Path(backup_app_raw) if backup_app_raw else None
        if original_app is not None and backup_app is not None and backup_app.is_dir():
            original_app.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(backup_app), str(original_app))

        # Deliberately not re-registering the menu-bar LaunchAgent. Its
        # ProgramArguments exec `ciao menubar`, and both that subcommand and the
        # rumps helper behind it were deleted in this release -- bootstrapping
        # it would leave launchd retrying a command that exits non-zero forever.
        # Rollback still restores the files from the backup so nothing is lost;
        # only the agent is left unloaded.
    except OSError as exc:
        return ServiceResult(False, "rollback", str(exc), asdict(runtime))
    return ServiceResult(
        True,
        "rollback",
        "Legacy companion restored from the migration backup.",
        {**asdict(runtime), "receipt": str(receipt_path)},
    )


def print_result(result: ServiceResult, *, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result.to_dict(), sort_keys=True))
    else:
        stream = sys.stdout if result.ok else sys.stderr
        print(result.message, file=stream)
    return 0 if result.ok else 1
