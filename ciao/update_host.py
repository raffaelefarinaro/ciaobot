"""The platform seam for the engine update transaction (#856, part 1).

:mod:`ciao.engine_update` owns one transaction — lock, drain, stop, swap, start,
verify, and one rollback generation — and the parts of it that are *not* that
transaction are the ones that name a supervisor. Which job owns the swap, how
the engine is stopped, what a staged environment looks like and where the
recovery interpreter lives are all questions this module answers, so that
`engine_update` can be a state machine with no `sys.platform` in it at all.

One protocol, one implementation per platform. :class:`MacUpdateHost` wraps
launchd, LaunchAgents and the POSIX environment layout exactly as `engine_update`
did before the seam existed, and :func:`current_update_host` picks the host for
this machine the way :func:`ciao.service_backend.current_backend` picks a
service backend. Any platform without a host raises
:class:`UnsupportedPlatformError`: that is the honest answer until one is
written, not a fallback to the macOS one.
"""

from __future__ import annotations

import contextlib
import logging
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol, Sequence

from ciao import macos_service
from ciao.os_support.private import make_private, mkstemp_private

if TYPE_CHECKING:
    from ciao.engine_update import Operation, UpdateError

logger = logging.getLogger(__name__)

# Imported, not re-spelled: the label the updater boots out has to be the one
# the installer registered, and a second literal here would drift silently.
SERVER_LABEL = macos_service.SERVER_LABEL
# The one-shot job that owns the swap. A sibling of `com.ciao.server`, not a
# child: `launchctl bootout` on the engine must not take the updater with it.
UPDATER_LABEL = "com.ciao.updater"
UPDATER_PLIST_NAME = "com.ciao.updater.plist"
# The durable recovery job: the net under the swap itself. A LaunchAgent of its
# own, written into the update state dir and run by launchd from the staged
# interpreter, because the window it covers is the one where
# `com.ciao.server`'s program no longer exists — launchd cannot start the
# engine, so nothing *inside* the engine can notice the swap is stranded. It is
# a sibling of both other jobs, and the only one that is re-run on a timer
# rather than once.
RECOVER_LABEL = "com.ciao.recover"
RECOVER_PLIST_NAME = "com.ciao.recover.plist"
# How often the recovery agent re-reads the record. Short enough that a reboot
# does not cost the operator an engine for long, long enough that a tick landing
# beside a live swap costs one `launchctl print`, one lock attempt and an exit.
_RECOVER_INTERVAL = 30

# Launchctl invocation: a list of arguments in, a completed process out. The
# default is `macos_service._launchctl`, so the only place launchd is ever
# named is the one place the installer already owns.
Launchctl = Callable[[list[str]], subprocess.CompletedProcess[str]]


class UnsupportedPlatformError(RuntimeError):
    """No update host exists for this platform."""


def _update_error(message: str) -> UpdateError:
    """``engine_update``'s ``UpdateError``, built without importing it here.

    ``UpdateError`` is the transaction's own error, and ``engine_update``
    imports this module at module level, so the import is taken at call time —
    long after both modules exist. Every refusal this module raises is caught by
    the transaction and recorded, so it has to be the class the transaction's
    callers already catch.
    """
    from ciao.engine_update import UpdateError

    return UpdateError(message)


class UpdateHost(Protocol):
    """The platform-specific half of the update transaction.

    Each method is one answer `engine_update` needs and cannot give itself.
    Implementations own the supervisor that runs the swap, the environment layout
    it swaps, and the interpreter a recovery job runs from; they must not own
    any of the transaction's ordering, because that ordering is what a rollback
    depends on and it is the same on every platform.
    """

    def engine_port(self) -> int:
        """The port the engine answers on, resolved the way this platform does."""
        ...

    def start_engine(self) -> Any:
        """Start the engine service; answer with something carrying ``.ok``."""
        ...

    def stop_engine(self, wait: Callable[[], bool] | None = None) -> bool:
        """Stop the engine service, then answer "is it gone?".

        ``wait`` is the transaction's own loopback probe, passed in rather than
        reimplemented here: it is the same question on every platform. A host
        that needs its own evidence the process has released its files (Windows'
        runtime lock) does that first and then runs ``wait``; the rollback calls
        this with no ``wait`` at all, because it waits as a step of its own.
        """
        ...

    def server_program(self) -> str | None:
        """The program the loaded service actually runs, or ``None``.

        ``None`` is "not loaded, or nothing usable was reported", and is the
        only case in which an on-disk definition may answer instead.
        """
        ...

    def spawn_updater(
        self,
        op: Operation,
        python: str,
        *,
        verb: str = "run-apply",
        args: Sequence[str] | None = None,
    ) -> None:
        """Start the one-shot job that owns the swap; raise ``UpdateError`` if not.

        The job runs ``python -I -m ciao.engine_update <verb> <args>`` from an
        environment the swap does not consume, and must be a *sibling* of the
        engine's own job: a helper the engine starts is either refused a
        breakaway or taken down with it.
        """
        ...

    def install_recovery_agent(self, op: Operation, python: str, root: Path) -> Path:
        """Load the durable recovery job for ``op``; return what was written."""
        ...

    def recovery_python(self, op: Operation, previous_env: Path) -> str:
        """The interpreter the recovery job runs from, as a path that exists."""
        ...

    def retire_recovery_agent(self, root: Path) -> None:
        """Unload the durable recovery job and delete what described it."""
        ...

    def updater_running(self) -> bool:
        """Whether a swap is running *right now*, as distinct from being loaded."""
        ...

    def swap_in_flight(self) -> bool:
        """Whether a live ``run-apply`` owns this platform's updater job."""
        ...

    def move_env(self, source: Path, dest: Path) -> None:
        """Move a whole environment, across filesystems when it has to."""
        ...

    def env_python(self, env: Path) -> Path:
        """The interpreter inside ``env``."""
        ...

    def find_staged_python(self, tool_dir: Path) -> Path:
        """The interpreter of the one environment staging built in ``tool_dir``."""
        ...

    def install_env(
        self, op: Operation, staged: Path, live: Path, *, bin_dir: Path, wheel: Path
    ) -> None:
        """Put ``staged`` where ``live`` was, and re-point what names it.

        ``op`` is in the signature because the record is what a host that
        *rebuilds* the environment rather than moving it installs from: the pins
        staging resolved, and the digest of the wheel they describe. A host that
        moves the staged environment does not read them.
        """
        ...

    def after_env_restored(self, live: Path, *, bin_dir: Path, wheel: Path) -> None:
        """Repair whatever names ``live``, after a rollback put it back."""
        ...

    def uv_candidates(self) -> list[str]:
        """The well-known locations of the ``uv`` the installer used."""
        ...

    def interrupt_signals(self) -> tuple[signal.Signals, ...]:
        """The signals that end an update the way a terminal does."""
        ...

    def redirect_detached_stdio(self, root: Path) -> None:
        """Give a detached helper somewhere to write, if it has none of its own."""
        ...


# ── the macOS host: launchd, LaunchAgents, POSIX env layout ─────────────


def _write_plist(plist: dict[str, Any], target: Path) -> Path:
    """Write ``plist`` to ``target`` atomically, owner-only; return ``target``.

    Owner-only, and through a temp file, because ``launchctl bootstrap`` reads
    it immediately afterwards and a half-written plist would be a job that never
    loads with nothing in the record to explain it.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = mkstemp_private(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(plist, handle)
        make_private(tmp)
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)
    return target


def _job_plist(
    op: Operation,
    python: str,
    *,
    label: str,
    verb: str,
    log_name: str,
    args: Sequence[str] | None = None,
    start_interval: int | None = None,
) -> dict[str, Any]:
    """The plist for one of the transaction's detached LaunchAgents.

    All of them are the same shape on purpose: a program that has to survive the
    engine being booted out, in its own process group, reading and writing nothing
    outside the update state dir. Three things differ, and each difference is
    load-bearing:

    * the *label* and the *verb*, because they are different jobs with different
      work to do;
    * ``start_interval``, which only the recovery agent takes. The updater runs
      the swap once and exits; the agent has to keep re-checking until it finds a
      stranded swap, because it also has to outlive the reboots and logouts it
      cannot observe. ``RunAtLoad`` is what makes it run at all after a reboot,
      and a one-shot job could not use either;
    * the log file, which is per job so that two of them running beside each
      other cannot interleave their output into something unreadable.

    Everything else — ``RunAtLoad``, ``KeepAlive`` false so a failed job never
    becomes a relaunch loop, ``AbandonProcessGroup`` so the engine's bootout
    cannot take it down and its exit cannot take the engine down — is shared
    because it is true of all of them.
    """
    log = Path(op.stage_dir) / log_name
    plist: dict[str, Any] = {
        "Label": label,
        "ProgramArguments": [
            python,
            "-I",
            "-m",
            "ciao.engine_update",
            verb,
            *(args if args is not None else ["--operation", op.id]),
        ],
        "RunAtLoad": True,
        "KeepAlive": False,
        # So the engine's bootout cannot reach the job's children, and the
        # job's exit cannot drag the engine down with it.
        "AbandonProcessGroup": True,
        "StandardOutPath": str(log),
        "StandardErrorPath": str(log),
    }
    if start_interval is not None:
        plist["StartInterval"] = start_interval
    return plist


def _write_job_plist(
    op: Operation,
    python: str,
    state_dir: Path,
    *,
    label: str,
    plist_name: str,
    verb: str,
    log_name: str,
    args: Sequence[str] | None = None,
    start_interval: int | None = None,
) -> Path:
    """Write one of the one-shot jobs' plist into the update state dir.

    In the state dir and nowhere else, deliberately: a plist in the LaunchAgents
    directory is re-registered by launchd at every login, and a one-shot job
    that came back to life after a reboot would run the swap again over an
    install the operator had already stopped trusting. The recovery agent is the
    exception, and says so in :func:`_recovery_plists`.
    """
    return _write_plist(
        _job_plist(
            op,
            python,
            label=label,
            verb=verb,
            log_name=log_name,
            args=args,
            start_interval=start_interval,
        ),
        state_dir / plist_name,
    )


def _write_updater_plist(
    op: Operation,
    python: str,
    state_dir: Path,
    *,
    verb: str = "run-apply",
    args: Sequence[str] | None = None,
) -> Path:
    """Write the one-shot updater LaunchAgent and return the path written.

    `verb` and `args` are what the job runs: `run-apply` for a staged swap and
    `run-recover` for one an earlier reboot left half-finished.
    """
    return _write_job_plist(
        op,
        python,
        state_dir,
        label=UPDATER_LABEL,
        plist_name=UPDATER_PLIST_NAME,
        verb=verb,
        log_name="updater.log",
        args=args,
    )


def _recovery_plists(root: Path) -> tuple[Path, Path]:
    """The durable agent's two plists: the record of it, and the one launchd reads.

    The state-dir copy is the one the transaction owns beside the operation
    record, and the one a reader (or a test) can find. The LaunchAgents copy is
    what makes the agent *durable*, and it is not redundant: a job registered with
    ``launchctl bootstrap`` lives in launchd's database for that login session
    only, and the login-time scan — the one thing that re-registers agents after
    a reboot or a logout — reads that directory and nothing else. Without it,
    ``RunAtLoad`` and ``StartInterval`` describe a job that disappears along with
    the crash it was installed for, which is the entire window this agent exists
    to cover.
    """
    return (
        root / RECOVER_PLIST_NAME,
        macos_service.default_launch_agents_dir() / RECOVER_PLIST_NAME,
    )


def _write_recover_plist(op: Operation, python: str, state_dir: Path) -> Path:
    """Write the durable recovery agent's plists; return the state-dir copy.

    The same job as the updater, on a timer and with a label of its own, so the
    two can be told apart in launchd and on disk: a recovery that re-ran the
    swap, or an apply that retired somebody else's job, would be a much worse bug
    than either one failing to load.

    The login-time copy is best effort. The state-dir copy is the durable record
    of what was installed, and an unwritable LaunchAgents directory must not cost
    the operator the net for the crash happening right now — launchd still runs
    the job this returns a path for, so the difference is only what survives a
    reboot.
    """
    plist = _job_plist(
        op,
        python,
        label=RECOVER_LABEL,
        verb="run-recover",
        log_name="recover.log",
        start_interval=_RECOVER_INTERVAL,
    )
    written = _write_plist(plist, state_dir / RECOVER_PLIST_NAME)
    try:
        _write_plist(
            plist, macos_service.default_launch_agents_dir() / RECOVER_PLIST_NAME
        )
    except OSError as exc:
        logger.warning(
            "could not write %s into the LaunchAgents directory, so the %s agent "
            "will not come back by itself after a reboot: %s",
            RECOVER_PLIST_NAME,
            RECOVER_LABEL,
            exc,
        )
    return written


def _retire_job(launch: Launchctl, domain_uid: int, label: str, *plists: Path) -> None:
    """Boot a job out and delete its plist(s), however either of those goes.

    Both steps, because neither is enough on its own. A plist alone leaves the job
    loaded in launchd with its ``StartInterval`` still ticking, so a settled update
    would keep re-checking its own record for as long as the job is registered —
    and, for the agent, would be re-registered at the next login on top of that.
    The bootout alone leaves a plist for that same next login to load.

    The plists go first, and that order is load-bearing: the recovery agent runs
    this on *itself*, so the bootout that follows is a SIGTERM this process may
    not survive. Unlinking first means the tidying-up cannot be the thing the
    signal interrupts.

    Nothing here raises and nothing here reports: the caller has already decided
    the outcome, and the machine is not improved by a recovery failing to tidy up
    after itself. Every caller reaches this only after the record has been settled
    and the engine started again.
    """
    for plist in plists:
        with contextlib.suppress(OSError):
            plist.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        launch(["bootout", f"gui/{domain_uid}/{label}"])


def _loaded_field(printed: str, key: str) -> str:
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
        for nxt in lines[index + 1 :]:
            parts.append(nxt)
            depth += nxt.count("(") + nxt.count("{") - nxt.count(")") - nxt.count("}")
            if depth <= 0:
                break
        return "\n".join(parts)
    return ""


def _loaded_tokens(printed: str, key: str) -> list[str]:
    """The tokens in a value ``launchctl print`` rendered for ``key``."""
    return [
        token
        for line in _loaded_field(printed, key).splitlines()
        for token in re.findall(r"[^\s,(){}]+", line)
    ]


def _loaded_program_argument(printed: str) -> str | None:
    """The program ``launchctl print`` says a loaded job runs, or ``None``.

    The first token of the ``program`` value in every shape launchd prints it
    (see :func:`_loaded_field`); nothing recognisable answers ``None``, which is
    evidence of nothing and so never refuses an update.
    """
    tokens = _loaded_tokens(printed, "program")
    return tokens[0].strip("\"'") if tokens else None


def _loaded_server_program(launch: Launchctl, domain_uid: int) -> str | None:
    """``com.ciao.server``'s program *as launchd would run it*, or ``None``.

    This is the interpreter the acceptance criterion is about, and the on-disk
    plist cannot be trusted for it: launchd loads a job once, so a plist
    rewritten afterwards — by a reinstall, by an operator's editor, by a
    `Ciaobot.app` installed over a terminal install — describes the next login
    while the running service still executes the old one. ``launchctl print``
    reports the loaded job, which is the one that has to agree with the
    receipt. ``None`` means "not loaded, or launchd said nothing usable", and
    only then may the on-disk plist answer instead.
    """
    try:
        printed = launch(["print", f"gui/{domain_uid}/{SERVER_LABEL}"])
    except OSError:
        return None
    if printed.returncode != 0:
        return None
    return _loaded_program_argument(printed.stdout or "")


# A `launchctl print` renders `pid = <n>` only while a process is running the
# job, and the job's `arguments` block says what that process is running. Both
# are needed to tell a live *swap* from a live *recovery*, because the two share
# a label: `recover_interrupted_apply` bootstraps `com.ciao.updater` in
# `run-recover` mode, so the recovery job sees its own pid in that print and
# would stand down against itself.
_LOADED_PID = re.compile(r"^[ \t]*pid = (?P<pid>\d+)\s*$", re.MULTILINE)


def _loaded_updater(launch: Launchctl, domain_uid: int) -> str:
    """What ``launchctl print`` says about ``com.ciao.updater``, or ``""``.

    Empty for every answer that names no loaded job: a non-zero exit, a
    launchctl that cannot be run at all, or output with no field in it. A
    one-shot LaunchAgent stays registered in launchd after its process exits, so
    this being non-empty says the job is *loaded*, and nothing more than that.
    """
    try:
        printed = launch(["print", f"gui/{domain_uid}/{UPDATER_LABEL}"])
    except OSError:
        return ""
    if printed.returncode != 0:
        return ""
    return printed.stdout or ""


def _console_scripts(wheel: Path) -> list[str]:
    """The console-script names the wheel's own metadata declares.

    Read from the verified wheel rather than from the staged environment, so
    the set of entry points the swap has to place is a fact about the release
    and not about how a particular uv happened to lay a tool env out. A wheel
    that cannot be read here is refused, because the alternative is an install
    whose ``ciao`` silently is not the release's own.
    """
    try:
        with zipfile.ZipFile(wheel) as archive:
            entries = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/entry_points.txt")
            ]
            # Deliberately not folded into the `except` below: a wheel with the
            # wrong number of entry-point files is a refusal of its own, and the
            # message should say which.
            if len(entries) != 1:
                raise _update_error(
                    f"the wheel {wheel.name} describes {len(entries)} "
                    "entry-point files, not one"
                )
            text = archive.read(entries[0]).decode("utf-8")
    except (OSError, UnicodeDecodeError, zipfile.BadZipFile) as exc:
        raise _update_error(
            f"could not read the entry points of {wheel.name}: {exc}"
        ) from exc

    names: list[str] = []
    in_console_scripts = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_console_scripts = stripped == "[console_scripts]"
        elif in_console_scripts and "=" in stripped:
            names.append(stripped.partition("=")[0].strip())
    if not names:
        raise _update_error(f"the wheel {wheel.name} declares no console scripts")
    return names


def _first_line(path: Path) -> str:
    """A file's first line, or ``""`` for anything unreadable.

    The one question both the shebang rewrite and its check below ask: does the
    first line of a file that just moved still name the env it was staged in?
    Unreadable reads as empty rather than raising — a file that cannot be read
    is not one this module can repair, and the entry points that matter are
    checked by the caller either way.
    """
    try:
        return path.read_text(encoding="utf-8").partition("\n")[0]
    except (OSError, UnicodeDecodeError):
        return ""


def _relocate_shebang(path: Path, staged_prefix: str, live_prefix: str) -> None:
    """Repoint a script's ``#!`` line from the staged env to the live one.

    A shebang is a fixed prefix of the first line, so this is a bounded textual
    replacement and not a guess at what a script means: a file whose first line
    is not a shebang, or one naming neither environment, is left exactly as it
    is.
    """
    first = _first_line(path)
    if not first.startswith("#!") or staged_prefix not in first:
        return
    _, newline, rest = path.read_text(encoding="utf-8").partition("\n")
    path.write_text(
        first.replace(staged_prefix, live_prefix) + newline + rest, encoding="utf-8", newline=""
    )


def _move_env(source: Path, dest: Path) -> None:
    """Move a whole environment, across filesystems when it has to.

    ``os.replace`` is a rename, which is what keeps the swap instant, but the
    tool directory and the update state can sit on different volumes, where a
    rename is refused outright; ``shutil.move`` copies in that case, slower but
    correct.
    """
    shutil.rmtree(dest, ignore_errors=True)
    try:
        os.replace(source, dest)
    except OSError:
        shutil.move(str(source), str(dest))


class MacUpdateHost:
    """launchd, LaunchAgents and the POSIX environment layout.

    Every macOS-only step of the transaction, and nothing else: the state
    machine, the drain, the ordering and the rollback all live in
    :mod:`ciao.engine_update` and are shared. This host is the part that knows
    the swap is a sibling LaunchAgent, that a job's domain is ``gui/<uid>``, and
    that a uv tool env keeps its interpreter in ``bin`` with POSIX shebangs
    naming it.

    ``launchctl`` and ``uid`` are arguments rather than looked up, because they
    are the seam the tests inject and a caller that already knows its own domain
    should not have to be told what it is.
    """

    def __init__(self, launchctl: Launchctl | None = None, uid: int | None = None) -> None:
        self._launchctl: Launchctl = launchctl or macos_service._launchctl
        if uid is None:
            # `os.getuid` does not exist on Windows, and this module is imported
            # there. Typed locally because the `getattr` is `Any` (AGENTS.md,
            # `no-any-return`).
            getuid: Callable[[], int] = getattr(os, "getuid")
            uid = getuid()
        self._uid = uid

    def _target(self, label: str) -> str:
        """The launchd job ``label`` in this user's domain."""
        return f"gui/{self._uid}/{label}"

    def _domain(self) -> str:
        """This user's launchd domain, the parent of every job target here."""
        return f"gui/{self._uid}"

    def _state_dir(self, op: Operation) -> Path:
        """Where ``op``'s job definitions live: the update state dir.

        Staging puts the operation's own directory *in* the state dir, one
        child per release version, so the state dir is the stage dir's parent.
        Read off the record rather than passed in, because the same two callers
        hand the same directory to :meth:`spawn_updater` and to every other
        definition write, and one derivation cannot disagree with another.
        """
        return Path(op.stage_dir).parent

    # ── the engine's own service ────────────────────────────────────

    def engine_port(self) -> int:
        """The port the engine answers on: its plist, its workspace .env, or the default.

        Resolved the same way every other launchd caller resolves it, so the
        updater probes the same port the tray and the service helpers do.
        """
        return macos_service.discover_runtime().port

    def start_engine(self) -> Any:
        """`launchctl enable` + `bootstrap` + `kickstart`, as the service helper does."""
        return macos_service.start_service()

    def stop_engine(self, wait: Callable[[], bool] | None = None) -> bool:
        """``launchctl bootout`` the engine, then run ``wait`` if it was given.

        ``bootout`` returns before launchd has finished with the job, which is
        why the forward path waits at all; the rollback waits as a step of its
        own, so it asks for the bootout alone.
        """
        self._launchctl(["bootout", self._target(SERVER_LABEL)])
        return wait() if wait is not None else True

    def server_program(self) -> str | None:
        """What the loaded ``com.ciao.server`` runs, or the plist's, or ``None``.

        The loaded job is the authority (see :func:`_loaded_server_program`): a
        service that is registered but not loaded is the only case where the
        on-disk plist may answer, and a missing, unreadable or argument-less
        plist answers ``None`` — evidence of nothing, which never refuses an
        update.
        """
        program = _loaded_server_program(self._launchctl, self._uid)
        if program is not None:
            return program
        path = macos_service.default_launch_agents_dir() / f"{SERVER_LABEL}.plist"
        try:
            with path.open("rb") as handle:
                loaded: Any = plistlib.load(handle)
        except (OSError, plistlib.InvalidFileException, ValueError):
            return None
        if not isinstance(loaded, dict):
            return None
        arguments = loaded.get("ProgramArguments")
        if not isinstance(arguments, list) or not arguments:
            return None
        return str(arguments[0])

    # ── the jobs that own the swap ──────────────────────────────────

    def spawn_updater(
        self,
        op: Operation,
        python: str,
        *,
        verb: str = "run-apply",
        args: Sequence[str] | None = None,
    ) -> None:
        """Write the one-shot updater LaunchAgent and have launchd run it.

        `verb` and `args` are what the job runs: `run-apply` for a staged swap
        and `run-recover` for one an earlier reboot left half-finished.

        bootout before bootstrap, as everywhere else here: a job left loaded from
        an earlier attempt would make bootstrap fail with "service already loaded"
        and leave nothing running at all. A job that is not there is the normal
        case, so the bootout's non-zero exit is ignored.
        """
        plist_path = _write_updater_plist(
            op, python, self._state_dir(op), verb=verb, args=args
        )
        self._launchctl(["bootout", self._target(UPDATER_LABEL)])
        bootstrap = self._launchctl(
            ["bootstrap", self._domain(), str(plist_path)]
        )
        if bootstrap.returncode != 0:
            detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
            raise _update_error(
                f"could not start the updater job: {detail or 'launchctl bootstrap failed'}"
            )

    def install_recovery_agent(self, op: Operation, python: str, root: Path) -> Path:
        """Load the durable recovery agent for ``op``; return the plist written.

        This one is started by launchd rather than by anything inside the engine, and
        re-checked on an interval, so it survives the crash, the reboot, the logout
        and the death of the job that installed it. That is the whole design, and it
        is needed because the window a swap can strand the machine in is one where
        ``com.ciao.server``'s own program names a file that does not exist: launchd
        cannot start the engine, and a recovery reached from inside the engine is
        then a recovery that cannot run.

        Which is why ``python`` is an explicit argument and not read off ``op``: the
        net is only worth anything if its program is a file that exists, so every
        caller has to place it in an env the swap does not *consume*.
        :func:`ciao.engine_update.apply_update` installs it while the live env is
        still intact, from the staged interpreter; ``run_apply`` re-points it at
        the previous env the moment the live one is renamed aside, because the swap
        then moves the staged env into the live env's place and the agent's program
        would be a dangling path forever after.

        bootout before bootstrap, as in :meth:`spawn_updater`: a job left loaded
        from an earlier attempt would make bootstrap fail with "service already
        loaded" and leave the machine with no net at all.
        """
        plist = _write_recover_plist(op, python, root)
        self._launchctl(["bootout", self._target(RECOVER_LABEL)])
        bootstrap = self._launchctl(["bootstrap", self._domain(), str(plist)])
        if bootstrap.returncode != 0:
            detail = (bootstrap.stderr or bootstrap.stdout or "").strip()
            raise _update_error(
                f"could not start the {RECOVER_LABEL} job: "
                f"{detail or 'launchctl bootstrap failed'}"
            )
        return plist

    def recovery_python(self, op: Operation, previous_env: Path) -> str:
        """The interpreter the recovery job runs from.

        Whichever of the two envs the swap has not consumed yet: the staged one
        while it is still only a directory, because that is what `run-apply` itself
        runs from; then the previous env, which exists from the moment the live one
        is renamed aside and is both the second-best thing to run the recovery *from*
        and the last one guaranteed to have `ciao` importable. Failing both, this
        process's own interpreter: it is the engine that is running the recovery, and
        the recovery job is a *sibling* of `com.ciao.server`, so the bootout that
        follows cannot take it down.
        """
        for candidate in (op.env_python, str(self.env_python(previous_env))):
            if candidate and Path(candidate).exists():
                return candidate
        return sys.executable

    def retire_recovery_agent(self, root: Path) -> None:
        """Unload the durable recovery agent and delete both of its plists."""
        _retire_job(self._launchctl, self._uid, RECOVER_LABEL, *_recovery_plists(root))

    def updater_running(self) -> bool:
        """Whether a process is running ``com.ciao.updater`` *right now*.

        Deliberately not "is the job loaded". A one-shot LaunchAgent stays loaded
        in launchd after its process exits, and nothing here ever boots it out, so
        the job left behind by the very crash recovery exists for would read as a
        live apply for ever — and a recovery that stands down from a dead swap is
        exactly the stranded machine this is here to prevent. ``launchctl print``
        names the pid only while something is running it, so that is what is asked.

        A launchctl that cannot be run at all answers False for the same reason a
        missing job does: there is no swap in flight, and the rollback that follows
        is the same total one `run_apply` would have run itself.
        """
        return _LOADED_PID.search(_loaded_updater(self._launchctl, self._uid)) is not None

    def swap_in_flight(self) -> bool:
        """Whether a live ``run-apply`` owns ``com.ciao.updater`` — a real swap.

        The pid alone is not enough, and getting this wrong strands the machine
        recovery exists to repair. ``run-recover`` runs under the *same* label
        (:func:`ciao.engine_update.recover_interrupted_apply` bootstraps it there),
        so the process executing ``recover_apply`` is `com.ciao.updater` and
        ``launchctl print`` reports its own live pid back to it. A job whose
        arguments name `run-recover` is this very recovery and must proceed; only
        a loaded *and* running job whose arguments name `run-apply` is a swap in
        flight.

        Anything launchd does not render — no pid, no arguments block — answers
        False, for the same reason a launchctl that cannot be run does: there is no
        swap in flight to race, the lock this job already holds is what actually
        keeps the two apart, and answering True here would leave every interrupted
        swap unrolled back.
        """
        printed = _loaded_updater(self._launchctl, self._uid)
        if _LOADED_PID.search(printed) is None:
            return False
        return "run-apply" in _loaded_tokens(printed, "arguments")

    # ── the environment being swapped ───────────────────────────────

    def move_env(self, source: Path, dest: Path) -> None:
        """Rename ``source`` onto ``dest``, copying only if the rename is refused."""
        _move_env(source, dest)

    def env_python(self, env: Path) -> Path:
        """A POSIX tool env's interpreter: ``<env>/bin/python``."""
        return env / "bin" / "python"

    def find_staged_python(self, tool_dir: Path) -> Path:
        """The interpreter of the one tool env ``uv`` built in ``tool_dir``.

        Read off the directory rather than spelled out: a tool env is named after
        the *distribution* (``ciaobot``), not the wheel file, and the rule uv
        normalises that name by is uv's business, not the transaction's. The tool
        dir belongs to one staging run and is emptied before it, so anything in it
        is what this run just installed — and an amount other than one is a
        refusal, because the record's ``env_python`` has to name the env the apply
        moves.
        """
        try:
            found = sorted({path.parent.parent for path in tool_dir.glob("*/bin/python")})
        except OSError as exc:
            raise _update_error(f"could not read the staged tool directory: {exc}") from exc
        if len(found) != 1:
            raise _update_error(
                f"expected one staged tool environment in {tool_dir}, found {len(found)}"
            )
        return self.env_python(found[0])

    def install_env(
        self, op: Operation, staged: Path, live: Path, *, bin_dir: Path, wheel: Path
    ) -> None:
        """Put the staged env where the live one was, and re-point what names it.

        A rename wherever the filesystem allows one, and nothing else: the staged
        env *is* the environment staging resolved and verified, so installing
        it again — from the network or from a cache — could only ever produce a
        different one (#611). ``op`` is unused here and is in the signature
        anyway, because a host that rebuilds the environment instead of moving
        it installs from the record's pins; see :meth:`UpdateHost.install_env`.

        A move breaks exactly two things *that matter* that name the env by absolute
        path, and both are repaired rather than tolerated:

        * the shebang of the scripts in ``<live_env>/bin``, which uv wrote naming
          the *staged* interpreter and which would otherwise be a program that
          cannot start (`bad interpreter`);
        * the entry points in ``bin_dir``, which uv places as links into the env
          and which would otherwise dangle.

        A real tool env leaves two more absolute paths behind, both stale-but-harmless
        and both left alone: the seven ``bin/activate*`` files, which each embed
        ``VIRTUAL_ENV="<env>"``, and the ``install-path`` entry in
        ``uv-receipt.toml``, which names the env's ``bin`` where it was staged.
        Nothing in this repo sources a tool env's ``activate``, and ``uv tool list``
        reads the requirement rather than the path.

        ``uv-receipt.toml`` itself rides along, so ``uv tool list`` still reports this
        install and the installer's own "was this installed by Ciaobot" guard keeps
        working.

        Anything that cannot be placed raises, which is what makes the caller's
        rollback the answer: an update that cannot put in place the environment it
        verified is not an update.
        """
        self.move_env(staged, live)
        # A shebang is POSIX and uv writes absolute POSIX paths into it, so the
        # prefixes are spelled with a literal separator rather than `os.sep`.
        staged_prefix = f"{staged}/bin/"
        live_prefix = f"{live}/bin/"
        try:
            scripts = sorted((live / "bin").iterdir())
        except OSError as exc:
            raise _update_error(f"the moved environment has no bin directory: {exc}") from exc
        for script in scripts:
            if script.is_file() and not script.is_symlink():
                _relocate_shebang(script, staged_prefix, live_prefix)
        # Fail closed on a survivor: `_relocate_shebang` only rewrites a first line
        # that *is* a shebang, so a launcher naming the interpreter some other way —
        # a wrapper whose first line is `exec <staged>/bin/python` — keeps naming a
        # directory the swap has just renamed away, and the entry point it leaves is
        # one that answers `bad interpreter`. Tolerated, that only surfaces as a new
        # engine that never comes up, at the end of a swap that has already cost the
        # operator their engine; refusing here is a rollback instead.
        for script in scripts:
            if (
                script.is_file()
                and not script.is_symlink()
                and staged_prefix in _first_line(script)
            ):
                raise _update_error(
                    f"the staged path is still in the first line of {script.name}, "
                    "so it would not start"
                )
        for name in _console_scripts(wheel):
            script = live / "bin" / name
            if not script.is_file():
                raise _update_error(f"the staged environment has no {name} entry point")
            shim = bin_dir / name
            if shim.is_symlink() or not shim.exists():
                # uv's own shape: a link into the env. The move broke it, because
                # it names the staged path, and a `bin_dir` that has lost the entry
                # point entirely gets the link it should have had.
                shim.unlink(missing_ok=True)
                shim.symlink_to(script)
            else:
                # A plain file here (a copy rather than a link, or a shim a previous
                # release wrote) is repaired in place: replacing it would throw away
                # whatever else it carries.
                _relocate_shebang(shim, staged_prefix, live_prefix)
            if not shim.is_file():
                raise _update_error(f"the {name} entry point {shim} does not resolve")

    def after_env_restored(self, live: Path, *, bin_dir: Path, wheel: Path) -> None:
        """Nothing to repair: the env put back is byte for byte the one moved aside.

        A macOS rollback renames ``previous-env`` back into the live path, so
        every absolute path that named the restored env — the scripts' shebangs,
        the entry-point links in ``bin_dir`` — named it before the swap and
        names it again now. The arguments are there because a host that rebuilds
        rather than renames has to re-place its launchers at this point.
        """

    # ── the tools this host needs ───────────────────────────────────

    def uv_candidates(self) -> list[str]:
        """The installer's well-known ``uv``: ``~/.local/bin/uv``.

        ``PATH`` and the receipt's own value are tried by the caller first; this
        is the last resort, the one path a machine that installed Ciaobot
        without help will have.
        """
        return [str(Path.home() / ".local" / "bin" / "uv")]

    def interrupt_signals(self) -> tuple[signal.Signals, ...]:
        """``SIGTERM`` and ``SIGHUP``: the two ways a ten-minute drain really ends."""
        return (signal.SIGTERM, signal.SIGHUP)

    def redirect_detached_stdio(self, root: Path) -> None:
        """Nothing to do: each job's plist names its own log file.

        The updater and the recovery agent both redirect into
        ``<stage>/updater.log`` and ``<stage>/recover.log``, per job, so two of
        them running beside each other cannot interleave their output into
        something nobody can read.
        """


def current_update_host() -> UpdateHost:
    """The update host for this platform. Reads ``sys.platform`` on every call."""
    if sys.platform == "darwin":
        return MacUpdateHost()
    raise UnsupportedPlatformError(
        f"Ciaobot has no update host for platform {sys.platform!r}; "
        "the engine update transaction is macOS-only so far."
    )
