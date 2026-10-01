"""The Windows update host: Task Scheduler siblings and an offline rebuild (#857).

Every Windows-only step of the engine update transaction in
:mod:`ciao.engine_update`, behind the same :class:`~ciao.update_host.UpdateHost`
protocol the macOS host implements. What differs from macOS, and why:

* **The swap runs in a sibling task.** ``\\Ciaobot\\Updater`` is a one-shot
  Task Scheduler task with no trigger, registered and started with
  ``schtasks /Create`` + ``/Run``. ``/End`` on ``\\Ciaobot\\Engine`` and the
  engine's ``KILL_ON_JOB_CLOSE`` Job Object cannot reach it, which is the
  property a child of the engine could not have.
* **A stop is confirmed by the waits, not by a rename.** Windows renames an
  environment directory while a running process still maps files from it, so
  a successful rename proves nothing. The engine is gone only when its port
  stops answering *and* its ``server.lock`` can be taken. Nothing here moves an
  environment before both hold.
* **The live env is rebuilt, not moved.** uv's launchers embed absolute paths,
  so a moved env cannot be started from. The live env is reinstalled in place,
  offline, from the wheel the record verified and the pins staging resolved
  (``--offline --constraint``), with ``--link-mode copy`` so its ``.pyd`` and
  ``.dll`` files are its own rather than hard links into uv's cache that an
  unrelated process can hold. This is a different guarantee from macOS's
  "moved, not rebuilt" (#611), accepted for Windows only (#857, maintainer
  decision 1). The staged env is never moved, so the updater and the recovery
  task always have an interpreter.
* **Deletes are best-effort.** Anything in the way of a rename is renamed into
  ``<state>/discarded`` first and deleted afterwards. A delete that fails is
  retried by the next ``\\Ciaobot\\Recover`` tick, and the recovery task is not
  retired until nothing is left (#857, maintainer decision 3). No rollback
  step depends on a delete succeeding.
"""

from __future__ import annotations

import contextlib
import datetime
import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Sequence

from ciao import engine_update, install_receipt, macos_service, windows_service
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.update_host import _console_scripts, _update_error

if TYPE_CHECKING:
    from ciao.engine_update import Operation

logger = logging.getLogger(__name__)

UPDATER_FILE_NAME = "Ciaobot-Updater.xml"
RECOVER_FILE_NAME = "Ciaobot-Recover.xml"
# Where anything in the way of a rename goes before it is deleted.
DISCARDED_DIR_NAME = "discarded"
# The log the updater and the recovery task write into under pythonw.exe,
# which has no stdout or stderr of its own.
LOG_NAME = "updater.log"
CONSTRAINTS_NAME = "constraints.txt"
# How long to wait for the engine to release `server.lock` after its port has
# closed. The spike measured 1.5 s on this hardware with Defender on.
_LOCK_POLL_S = 0.5

Schtasks = Callable[..., "subprocess.CompletedProcess[str]"]
Runner = Callable[..., Any]
Sleep = Callable[[float], None]
Clock = Callable[[], float]


def _local_now() -> str:
    """Local wall-clock time in the form a task trigger's StartBoundary takes."""
    return datetime.datetime.now().replace(microsecond=0).isoformat()


def _rmtree_best_effort(path: Path) -> bool:
    """Delete ``path`` once; True when nothing is left.

    One attempt that clears the read-only bit uv leaves on some files, and no
    retry loop: what an unrelated process holds open stays held for as long as
    that process runs, so retrying here only delays the rollback. The next
    recovery tick tries again (see :meth:`WindowsUpdateHost.retire_recovery_agent`).
    """

    def clear_readonly(func: Callable[..., Any], target: str, _exc: BaseException) -> None:
        with contextlib.suppress(OSError):
            os.chmod(target, 0o700)
            func(target)

    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=clear_readonly)
    return not path.exists()


def _pins(env_freeze: str) -> str:
    """The pins staging resolved, minus the package being installed itself.

    ``uv pip freeze`` lists ``ciaobot`` too, as the wheel it was installed from;
    constraining the wheel against its own line is circular, and the wheel's
    digest is what pins it.
    """
    keep: list[str] = []
    for line in env_freeze.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name = stripped.split("@", 1)[0].split("==", 1)[0].strip()
        if name.lower().replace("_", "-") == "ciaobot":
            continue
        keep.append(stripped)
    return "\n".join(keep) + "\n"


class WindowsUpdateHost:
    """Task Scheduler, the uv tool layout on Windows, and an offline rebuild.

    ``schtasks``, ``run``, ``sleep`` and ``clock`` are the seams the tests
    inject. ``state_dir`` is the update state dir, which the host needs for
    the update lock (:meth:`updater_running`) and for the discarded-env
    directory; it defaults to the transaction's own. ``task_dir`` is where the
    engine task's definition was written, which names its workspace.
    """

    def __init__(
        self,
        *,
        schtasks: Schtasks | None = None,
        run: Runner = subprocess.run,
        state_dir: Path | None = None,
        task_dir: Path | None = None,
        sleep: Sleep = time.sleep,
        clock: Clock = time.monotonic,
    ) -> None:
        self._schtasks: Schtasks = schtasks or windows_service._schtasks
        self._run = run
        self._state_dir = state_dir
        self._task_dir = task_dir
        self._sleep = sleep
        self._clock = clock

    # ── where things are ────────────────────────────────────────────

    def _state_root(self) -> Path:
        return self._state_dir or engine_update.default_state_dir()

    def _discarded_root(self) -> Path:
        return self._state_root() / DISCARDED_DIR_NAME

    def _workspace(self) -> Path | None:
        """The workspace the registered engine task serves, if it says."""
        task_dir = self._task_dir or windows_service.default_task_dir()
        return windows_service.task_workspace(task_dir / windows_service.TASK_FILE_NAME)

    def _dotenv(self, workspace: Path | None) -> dict[str, str]:
        return macos_service.read_dotenv(workspace / ".env") if workspace else {}

    def _server_lock(self) -> Path:
        """The engine's ``server.lock``: ``CIAO_RUNTIME_ROOT`` or ``<workspace>/.runtime``.

        Resolved the way the engine resolves it, from the workspace's ``.env``. A
        machine whose task names no workspace has no engine this host can watch,
        and is refused rather than guessed at: a stop that cannot be confirmed
        is a stop that did not happen.
        """
        workspace = self._workspace()
        if workspace is None:
            raise _update_error(
                f"the {windows_service.TASK_NAME} task names no workspace, so the "
                "engine's runtime lock cannot be found"
            )
        runtime_raw = self._dotenv(workspace).get("CIAO_RUNTIME_ROOT", "").strip()
        runtime = Path(runtime_raw).expanduser() if runtime_raw else Path(".runtime")
        if not runtime.is_absolute():
            runtime = workspace / runtime
        return runtime / "server.lock"

    def _engine_holds_lock(self) -> bool:
        """Whether a process holds the engine's ``server.lock`` right now."""
        path = self._server_lock()
        try:
            handle = path.open("rb")
        except FileNotFoundError:
            # No engine has ever started in this runtime directory: the lock
            # file is created on first start and deliberately never removed.
            return False
        except OSError as exc:
            raise _update_error(f"could not open {path}: {exc}") from exc
        with handle:
            try:
                lock_exclusive(handle.fileno(), blocking=False)
            except BlockingIOError:
                return True
            except OSError as exc:
                raise _update_error(f"could not probe {path}: {exc}") from exc
            unlock(handle.fileno())
            return False

    def _wait_lock_free(self) -> bool:
        deadline = self._clock() + engine_update._STOP_TIMEOUT
        while True:
            if not self._engine_holds_lock():
                return True
            if self._clock() >= deadline:
                return False
            self._sleep(_LOCK_POLL_S)

    # ── the engine's own service ────────────────────────────────────

    def engine_port(self) -> int:
        """``PWA_PORT`` from the task workspace's ``.env``, or the default port."""
        raw = self._dotenv(self._workspace()).get("PWA_PORT", "").strip()
        try:
            port = int(raw) if raw else macos_service.DEFAULT_PORT
        except ValueError:
            return macos_service.DEFAULT_PORT
        return port if 1 <= port <= 65535 else macos_service.DEFAULT_PORT

    def start_engine(self) -> Any:
        """``schtasks /Run`` on the engine task, unless it already answers.

        The engine task restarts on failure once a minute, so by the time a
        rollback starts it the task may have started it already, and ``/Run``
        on a running ``IgnoreNew`` task is not the success it looks like.
        Whichever version answers is checked by the transaction's readiness
        wait, not here.
        """
        if macos_service.server_reachable(self.engine_port()):
            return macos_service.ServiceResult(
                True, "start", "Ciaobot engine is already running.", {"task": windows_service.TASK_NAME}
            )
        try:
            completed = self._schtasks("/Run", "/TN", windows_service.TASK_NAME)
        except windows_service.WindowsServiceError as exc:
            return macos_service.ServiceResult(False, "start", str(exc), {})
        if completed.returncode != 0:
            failure = windows_service._failure("/Run", completed)
            return macos_service.ServiceResult(False, "start", str(failure), {})
        return macos_service.ServiceResult(
            True, "start", "Ciaobot engine started.", {"task": windows_service.TASK_NAME}
        )

    def stop_engine(self, wait: Callable[[], bool] | None = None) -> bool:
        """``/End`` the engine task, then wait until its port and lock are free.

        A non-zero ``/End`` is not an error: it is also what an engine that was
        not running answers, and the waits decide either way. The lock wait
        runs even when ``wait`` is None (the rollback's call), because on
        Windows the lock is the only evidence the process has let go of the
        environment it ran from.
        """
        try:
            self._schtasks("/End", "/TN", windows_service.TASK_NAME)
        except windows_service.WindowsServiceError as exc:
            raise _update_error(str(exc)) from exc
        if wait is not None and not wait():
            return False
        return self._wait_lock_free()

    def server_program(self) -> str | None:
        """What the registered ``\\Ciaobot\\Engine`` runs, or None."""
        return windows_service.task_command(windows_service.TASK_NAME, runner=self._schtasks)

    # ── the tasks that own the swap ─────────────────────────────────

    def _register(self, xml: str, target: Path, name: str) -> None:
        """Write ``xml`` (UTF-16 with BOM) to ``target`` and register it as ``name``."""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(xml.encode("utf-16"))
        try:
            completed = self._schtasks("/Create", "/TN", name, "/XML", str(target), "/F")
        except windows_service.WindowsServiceError as exc:
            raise _update_error(str(exc)) from exc
        if completed.returncode != 0:
            raise _update_error(str(windows_service._failure("/Create", completed)))

    def _job_arguments(self, op: Operation, verb: str, args: Sequence[str] | None) -> list[str]:
        return ["-I", "-m", "ciao.engine_update", verb, *(args if args is not None else ["--operation", op.id])]

    def spawn_updater(
        self,
        op: Operation,
        python: str,
        *,
        verb: str = "run-apply",
        args: Sequence[str] | None = None,
    ) -> None:
        """Register ``\\Ciaobot\\Updater`` for ``verb`` and ``/Run`` it once.

        ``pythonw.exe`` beside ``python``, so the task opens no console window;
        :meth:`redirect_detached_stdio` gives it a log instead. ``/F`` replaces
        the definition an earlier update left registered.
        """
        try:
            xml = windows_service.render_oneshot_task_xml(
                python=windows_service.windowless_python(python),
                arguments=self._job_arguments(op, verb, args),
                workdir=op.stage_dir,
                user=windows_service.current_user(),
            )
        except (ValueError, windows_service.WindowsServiceError) as exc:
            raise _update_error(f"could not describe the updater task: {exc}") from exc
        self._register(xml, Path(op.stage_dir).parent / UPDATER_FILE_NAME, windows_service.UPDATER_TASK_NAME)
        try:
            completed = self._schtasks("/Run", "/TN", windows_service.UPDATER_TASK_NAME)
        except windows_service.WindowsServiceError as exc:
            raise _update_error(str(exc)) from exc
        if completed.returncode != 0:
            raise _update_error(
                f"could not start the updater task: {windows_service._failure('/Run', completed)}"
            )

    def install_recovery_agent(self, op: Operation, python: str, root: Path) -> Path:
        """Register ``\\Ciaobot\\Recover``: at logon and once a minute.

        Always from the staged interpreter, whatever ``python`` names. On macOS
        the transaction re-points the agent at ``previous-env`` once the live
        env is renamed, because the staged env is about to be moved; on Windows
        the staged env is never moved, and ``previous-env`` is the directory a
        rollback renames back into the live place, which a task running from it
        would still be mapping.
        """
        interpreter = op.env_python or python
        try:
            xml = windows_service.render_recover_task_xml(
                python=windows_service.windowless_python(interpreter),
                arguments=self._job_arguments(op, "run-recover", None),
                workdir=op.stage_dir,
                user=windows_service.current_user(),
                start=_local_now(),
            )
        except (ValueError, windows_service.WindowsServiceError) as exc:
            raise _update_error(f"could not describe the recovery task: {exc}") from exc
        target = root / RECOVER_FILE_NAME
        self._register(xml, target, windows_service.RECOVER_TASK_NAME)
        return target

    def recovery_python(self, op: Operation, previous_env: Path) -> str:
        """The staged interpreter, which the Windows swap never moves.

        ``previous_env`` is not a candidate here (see
        :meth:`install_recovery_agent`). This process's own interpreter is the
        last resort, as on macOS: the recovery task is a sibling of the engine,
        so stopping the engine cannot take it down.
        """
        if op.env_python and Path(op.env_python).exists():
            return op.env_python
        return sys.executable

    def _leftovers(self, root: Path) -> list[Path]:
        """What a best-effort delete has not managed to remove yet."""
        found: list[Path] = []
        discarded = root / DISCARDED_DIR_NAME
        with contextlib.suppress(OSError):
            found.extend(sorted(discarded.iterdir()))
        op = engine_update.read_operation(root)
        if op is not None:
            keep = os.path.abspath(op.stage_dir)
            with contextlib.suppress(OSError):
                for child in sorted(root.iterdir()):
                    previous = child / engine_update.PREVIOUS_ENV_NAME
                    if os.path.abspath(child) != keep and previous.exists():
                        found.append(previous)
        return found

    def retire_recovery_agent(self, root: Path) -> None:
        """Delete what is left to delete; retire ``\\Ciaobot\\Recover`` once it is all gone.

        The once-a-minute recovery task is also how a failed delete is retried:
        while a discarded env or a superseded ``previous-env`` is still on disk,
        the task stays registered and the next tick runs this again. A tick with
        nothing pending never reaches here, because the task is gone by then.
        """
        for path in self._leftovers(root):
            _rmtree_best_effort(path)
        with contextlib.suppress(OSError):
            (root / DISCARDED_DIR_NAME).rmdir()
        remaining = self._leftovers(root)
        if remaining:
            logger.warning(
                "keeping %s to retry deleting %s",
                windows_service.RECOVER_TASK_NAME,
                ", ".join(str(path) for path in remaining),
            )
            return
        with contextlib.suppress(OSError):
            (root / RECOVER_FILE_NAME).unlink(missing_ok=True)
        with contextlib.suppress(windows_service.WindowsServiceError):
            self._schtasks("/Delete", "/TN", windows_service.RECOVER_TASK_NAME, "/F")

    def updater_running(self) -> bool:
        """Whether a swap or a recovery holds the update lock right now.

        On Windows the lock *is* the answer: Task Scheduler can say whether the
        updater task is running only in localized output, and every process that
        works on the record holds this lock while it does.
        """
        try:
            handle = engine_update.acquire_lock(self._state_root())
        except engine_update.UpdateInProgress:
            return True
        engine_update.release_lock(handle)
        return False

    def swap_in_flight(self) -> bool:
        """False: the caller already holds the update lock, which is the guard."""
        return False

    # ── the environment being swapped ───────────────────────────────

    def move_env(self, source: Path, dest: Path) -> None:
        """Rename ``source`` onto ``dest``, once the engine has let go of both.

        Refused while the engine holds its lock, because Windows would allow the
        rename regardless. Whatever is already at ``dest`` (a half-installed env
        the rollback could not delete, typically) is renamed into the discarded
        directory first and deleted best-effort, so the rename never depends on
        a delete succeeding. A rename across volumes is refused rather than
        copied: on Windows the tool dir and the update state both live under the
        user profile, and a copy of a whole env is not what a rollback should
        quietly turn into.
        """
        if self._engine_holds_lock():
            raise _update_error(f"the engine is still running, so {source} was not moved")
        if dest.exists():
            discarded = self._discarded_root()
            discarded.mkdir(parents=True, exist_ok=True)
            aside = discarded / f"{engine_update._stamp()}-{dest.name}"
            try:
                os.replace(dest, aside)
            except OSError as exc:
                raise _update_error(f"could not move {dest} out of the way: {exc}") from exc
            _rmtree_best_effort(aside)
        try:
            os.replace(source, dest)
        except OSError as exc:
            raise _update_error(f"could not move {source} to {dest}: {exc}") from exc

    def env_python(self, env: Path) -> Path:
        """A Windows tool env's interpreter: ``<env>/Scripts/python.exe``."""
        return env / "Scripts" / "python.exe"

    def find_staged_python(self, tool_dir: Path) -> Path:
        """The interpreter of the one tool env ``uv`` built in ``tool_dir``."""
        try:
            found = sorted({path.parent.parent for path in tool_dir.glob("*/Scripts/python.exe")})
        except OSError as exc:
            raise _update_error(f"could not read the staged tool directory: {exc}") from exc
        if len(found) != 1:
            raise _update_error(
                f"expected one staged tool environment in {tool_dir}, found {len(found)}"
            )
        return self.env_python(found[0])

    def _uv(self) -> str:
        receipt = install_receipt.read_receipt()
        return engine_update.find_uv(receipt.uv if receipt is not None else "", host=self)

    def install_env(
        self, op: Operation, staged: Path, live: Path, *, bin_dir: Path, wheel: Path
    ) -> None:
        """Rebuild the live env in place, offline, from the verified inputs.

        ``staged`` is not read: the staged env stays where staging built it, as
        the interpreter the updater and the recovery task run from. What is
        installed is the wheel whose digest ``run_apply`` re-checked before the
        engine was stopped, constrained to the pins staging resolved, from uv's
        cache and with no network (``--offline``). A cache purged since staging
        makes uv fail here, which the transaction answers with a rollback.

        uv runs with the stage dir as its working directory and the constraints
        and wheel as relative paths: uv splits an absolute ``--constraint`` path
        that contains a space, which every Windows profile named
        "First Last" has (#857 spike).
        """
        if not op.env_freeze.strip():
            raise _update_error("the update record has no resolved pins to install from")
        stage = Path(op.stage_dir)
        (stage / CONSTRAINTS_NAME).write_text(_pins(op.env_freeze), encoding="utf-8", newline="")
        python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
        started = self._clock()
        try:
            self._run(
                [
                    self._uv(),
                    "tool",
                    "install",
                    "--offline",
                    "--force",
                    "--link-mode",
                    "copy",
                    "--python",
                    python_version,
                    "--constraint",
                    CONSTRAINTS_NAME,
                    os.path.relpath(wheel, stage),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=engine_update._UV_TIMEOUT,
                cwd=str(stage),
                env={
                    **os.environ,
                    "UV_TOOL_DIR": str(live.parent),
                    "UV_TOOL_BIN_DIR": str(bin_dir),
                },
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or "").strip()
            raise _update_error(
                f"the offline reinstall failed{': ' + detail[-2000:] if detail else ''}"
            ) from exc
        except (OSError, subprocess.SubprocessError) as exc:
            raise _update_error(f"could not run uv: {exc}") from exc
        logger.info("offline reinstall took %.1fs", self._clock() - started)
        if not self.env_python(live).is_file():
            raise _update_error(f"the reinstall left no interpreter at {self.env_python(live)}")
        for name in _console_scripts(wheel):
            if not (bin_dir / f"{name}.exe").is_file():
                raise _update_error(f"the reinstall left no {name}.exe in {bin_dir}")

    def after_env_restored(self, live: Path, *, bin_dir: Path, wheel: Path) -> None:
        """Copy the restored env's launchers back over the bin dir's.

        What uv itself does on Windows: an entry point in the bin dir is a copy
        of the env's own ``Scripts\\<name>.exe``, which names the env's
        interpreter by absolute path. The reinstall replaced those copies with
        the new env's, so after the old env is renamed back they have to be the
        old env's again. A script the new release added and the old env lacks
        names the discarded env, and is removed rather than left to fail.
        """
        for name in _console_scripts(wheel):
            source = live / "Scripts" / f"{name}.exe"
            target = bin_dir / f"{name}.exe"
            if source.is_file():
                shutil.copy2(source, target)
            else:
                target.unlink(missing_ok=True)

    # ── the tools this host needs ───────────────────────────────────

    def uv_candidates(self) -> list[str]:
        """The installer's well-known ``uv``: ``~/.local/bin/uv.exe``."""
        return [str(Path.home() / ".local" / "bin" / "uv.exe")]

    def interrupt_signals(self) -> tuple[signal.Signals, ...]:
        """``SIGTERM`` and ``SIGBREAK`` (Ctrl-Break, and a console closing)."""
        sigbreak: signal.Signals = getattr(signal, "SIGBREAK")
        return (signal.SIGTERM, sigbreak)

    def redirect_detached_stdio(self, root: Path) -> None:
        """Under pythonw.exe, append stdout and stderr to ``<root>/updater.log``.

        The updater and the recovery task share the file. Lines are written
        whole (line-buffered), and a recovery tick that finds the lock held
        writes one line and exits, so the two cannot garble each other.
        """
        if sys.stderr is not None:
            return
        root.mkdir(parents=True, exist_ok=True)
        log = open(root / LOG_NAME, "a", encoding="utf-8", newline="", buffering=1)
        sys.stdout = sys.stderr = log
        logging.basicConfig(stream=log, level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
