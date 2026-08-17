"""Subprocess-only background runs: the primitive between ``nohup`` and a delegate.

A background run is one command, launched with ``create_subprocess_exec`` (no
shell, ever), whose output lands in a rotating log under
``.runtime/background/<run_id>.log`` and whose completion wakes the chat that
started it. There is no model in the loop: the cost of a run is the command
itself plus the one wake turn.

Shape borrowed from two existing modules:

* :mod:`ciao.loops` for the registry side — a JSON store
  (``.runtime/background/state.json``) plus a manager that owns runtime state
  and a janitor task.
* :mod:`ciao.job_runs` for the recorder side — every finished run is also
  recorded as a ``background_run`` job so the Automation page shows it, and the
  log rotates on the same 2 MB threshold as ``job_runs._trim_if_large``.

Two deliberate divergences from the sketch in issue #282:

* **No 5-second poller.** The engine owns the subprocess, so ``proc.wait()`` is
  exact and fires the instant the child exits. A poller would be strictly
  slower and would also have to solve PID reuse. The janitor task that does
  exist runs every few minutes and only prunes; it is not how completion is
  detected.
* **A restart does not adopt orphans.** After a hard crash the recorded PID may
  have been reused by an unrelated process, and there is no dependency-free way
  to prove identity. Signalling it blind is worse than leaking, so a run left
  non-terminal by a crash resolves to ``error`` and wakes its chat with the PID
  and log path rather than being killed. The graceful path (which is how
  Ciaobot restarts) has no orphans at all: :meth:`BackgroundRunner.stop`
  terminates every live run before the engine exits.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from ciao import job_runs
from ciao.jsonio import read_json_dict

logger = logging.getLogger(__name__)

# Log rotation mirrors job_runs: trim once past ~2 MB, keeping the tail.
MAX_LOG_BYTES = 2 * 1024 * 1024
KEEP_LOG_BYTES = 1024 * 1024
_TRUNCATION_MARKER = b"[ciaobot] ... earlier output trimmed ...\n"

DEFAULT_TIMEOUT_S = 1800
MAX_TIMEOUT_S = 24 * 60 * 60
# Ceiling on live runs per chat. A runaway fan-out of subprocesses is cheaper
# than a fan-out of delegates but still real, so the control plane refuses past
# this (same reasoning as _MAX_ACTIVE_DELEGATES).
MAX_ACTIVE_RUNS_PER_CHAT = 5
# SIGTERM, then SIGKILL this many seconds later.
CANCEL_GRACE_SECONDS = 5.0
# Terminal runs older than this are dropped from the registry with their logs.
RETENTION_DAYS = 7
JANITOR_SECONDS = 300.0
TAIL_LINES = 50
# Bytes read off the end of a log to build the tail. 50 long lines fit easily.
_TAIL_READ_BYTES = 128 * 1024

MAX_ARGS = 64
MAX_ARG_CHARS = 4096
MAX_LABEL_CHARS = 120
MAX_ENV_VARS = 32
MAX_ENV_VALUE_CHARS = 4096

_ENV_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Environment names a caller may never set. The dynamic-loader hooks turn any
# command into arbitrary in-process code, and the session token is the chat's
# own control-plane capability: handing it to a subprocess would let a command
# call back into the MCP surface with the caller's authority, outliving the
# turn that was approved.
_FORBIDDEN_ENV_KEYS = frozenset({
    "LD_PRELOAD",
    "LD_LIBRARY_PATH",
    "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES",
    "DYLD_LIBRARY_PATH",
    "DYLD_FRAMEWORK_PATH",
    "CIAO_MCP_SESSION_TOKEN",
})

# Stripped from the inherited environment before the child sees it. Same
# reasoning as above, plus the PWA's own operator credentials: a background
# command is model-authored, and nothing about "run this script" needs the
# keys that authenticate the server itself.
_STRIPPED_ENV_KEYS = frozenset({
    "CIAO_MCP_SESSION_TOKEN",
    "PWA_AUTH_TOKEN",
})

STATE_DIR_NAME = "background"
STATE_FILE_NAME = "state.json"

_TERMINAL_STATUSES = frozenset({"ok", "error", "cancelled"})


class BackgroundRunError(ValueError):
    """Validation/limit failure with a stable code for the MCP boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now_utc().isoformat(timespec="seconds")


def _parse_iso_utc(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(slots=True)
class BackgroundRun:
    """One persisted background command, owned by the chat that started it."""

    run_id: str
    parent_chat_id: str
    project_id: str = ""
    workspace: str = ""
    label: str = ""
    cmd: list[str] = field(default_factory=list)
    cwd: str = ""
    pid: int = 0
    timeout_s: int = DEFAULT_TIMEOUT_S
    started_at: str = ""
    ended_at: str = ""
    # "queued" | "running" | "ok" | "error" | "cancelled"
    status: str = "queued"
    exit_code: int | None = None
    error: str = ""

    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = asdict(self)
        return data


# ── validation ────────────────────────────────────────────────────────────
# Kept as module functions so they can be tested without a runner, and so the
# rules live in one readable block rather than scattered through start().


def validate_cmd(cmd: Any) -> list[str]:
    """Return a safe argv list, or raise.

    A list is the only accepted shape on purpose. Accepting a string would
    force a split — and every split is either wrong (``shlex``) or a shell
    (``shell=True``). The caller already knows its own arguments.
    """
    if isinstance(cmd, str) or not isinstance(cmd, (list, tuple)):
        raise BackgroundRunError(
            "invalid_cmd",
            "cmd must be a list of arguments, e.g. [\"./script.sh\", \"--flag\"]. "
            "A single string is rejected because splitting it would mean a shell.",
        )
    argv = list(cmd)
    if not argv:
        raise BackgroundRunError("invalid_cmd", "cmd must have at least one element.")
    if len(argv) > MAX_ARGS:
        raise BackgroundRunError("invalid_cmd", f"cmd may have at most {MAX_ARGS} arguments.")
    out: list[str] = []
    for item in argv:
        if not isinstance(item, str):
            raise BackgroundRunError("invalid_cmd", "every cmd argument must be a string.")
        if "\x00" in item:
            raise BackgroundRunError("invalid_cmd", "cmd arguments may not contain NUL.")
        if len(item) > MAX_ARG_CHARS:
            raise BackgroundRunError(
                "invalid_cmd", f"a cmd argument exceeds {MAX_ARG_CHARS} characters."
            )
        out.append(item)
    if not out[0].strip():
        raise BackgroundRunError("invalid_cmd", "the executable in cmd[0] is empty.")
    return out


def resolve_cwd(workspace_root: Path, cwd: str) -> Path:
    """Resolve the working directory, confined to the workspace root.

    Same rule as ``CiaoControlPlane._safe_relative``: relative paths only, and
    the resolved target must stay under the root. ``..`` and symlink escapes
    both fail the ``is_relative_to`` check because the path is resolved first.
    """
    root = Path(workspace_root).resolve()
    raw = (cwd or "").strip()
    if not raw:
        return root
    if "\x00" in raw:
        raise BackgroundRunError("invalid_cwd", "cwd may not contain NUL.")
    candidate = Path(raw)
    if candidate.is_absolute():
        raise BackgroundRunError(
            "invalid_cwd", "Use a path relative to the active workspace root."
        )
    target = (root / candidate).resolve()
    if not target.is_relative_to(root):
        raise BackgroundRunError("cwd_forbidden", "cwd resolves outside the workspace root.")
    if not target.is_dir():
        raise BackgroundRunError("cwd_not_found", f"'{raw}' is not a directory in the workspace.")
    return target


def resolve_executable(argv0: str, cwd: Path, workspace_root: Path) -> str:
    """Resolve ``cmd[0]`` up front, using the *server's* PATH.

    Resolving before the caller's ``env`` overrides are applied is the point: a
    ``PATH`` override in ``env`` then cannot redirect which binary actually
    runs, only what the command itself looks up later.

    Three shapes are accepted, and the resolved target must exist and be
    executable in all three — a failed launch should be a clear validation
    error at the tool boundary, not a run that dies one line into its log:

    * a bare program name (``pytest``), looked up on PATH;
    * a relative path (``./build.sh``, ``scripts/x.py``), resolved inside the
      run directory and confined to the workspace root, exactly like ``cwd``;
    * an absolute path (``/usr/bin/python3``). Not confined, because PATH
      lookup already reaches outside the workspace and pretending otherwise
      would be friction without a boundary.
    """
    root = Path(workspace_root).resolve()
    candidate = Path(argv0)
    if candidate.is_absolute():
        target = candidate.resolve()
    elif "/" in argv0:
        target = (cwd / candidate).resolve()
        if not target.is_relative_to(root):
            raise BackgroundRunError(
                "cmd_forbidden", "cmd[0] resolves outside the workspace root."
            )
    else:
        found = shutil.which(argv0)
        if not found:
            raise BackgroundRunError("cmd_not_found", f"'{argv0}' was not found on PATH.")
        return found
    if not target.is_file() or not os.access(target, os.X_OK):
        raise BackgroundRunError("cmd_not_found", f"'{argv0}' is not an executable file.")
    return str(target)


def build_env(overrides: Any, *, run_id: str, workspace: str) -> dict[str, str]:
    """Inherit the server environment minus secrets, then apply overrides."""
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _STRIPPED_ENV_KEYS
    }
    if overrides:
        if not isinstance(overrides, dict):
            raise BackgroundRunError("invalid_env", "env must be an object of name/value pairs.")
        if len(overrides) > MAX_ENV_VARS:
            raise BackgroundRunError("invalid_env", f"env may set at most {MAX_ENV_VARS} names.")
        for raw_key, raw_value in overrides.items():
            key = str(raw_key)
            if not _ENV_KEY_RE.fullmatch(key):
                raise BackgroundRunError(
                    "invalid_env",
                    f"'{key}' is not a valid environment variable name.",
                )
            if key in _FORBIDDEN_ENV_KEYS:
                raise BackgroundRunError(
                    "env_forbidden",
                    f"'{key}' cannot be set for a background run.",
                )
            if raw_value is None:
                raise BackgroundRunError("invalid_env", f"env['{key}'] has no value.")
            if isinstance(raw_value, bool) or not isinstance(raw_value, (str, int, float)):
                raise BackgroundRunError(
                    "invalid_env", f"env['{key}'] must be a string or number."
                )
            value = str(raw_value)
            if "\x00" in value:
                raise BackgroundRunError("invalid_env", f"env['{key}'] may not contain NUL.")
            if len(value) > MAX_ENV_VALUE_CHARS:
                raise BackgroundRunError(
                    "invalid_env",
                    f"env['{key}'] exceeds {MAX_ENV_VALUE_CHARS} characters.",
                )
            env[key] = value
    env["CIAO_BACKGROUND_RUN_ID"] = run_id
    if workspace:
        env["CIAO_ACTIVE_WORKSPACE"] = workspace
    return env


def validate_timeout(timeout_s: Any) -> int:
    try:
        value = int(timeout_s)
    except (TypeError, ValueError) as exc:
        raise BackgroundRunError("invalid_timeout", "timeout_s must be an integer.") from exc
    if value <= 0:
        raise BackgroundRunError("invalid_timeout", "timeout_s must be positive.")
    return min(value, MAX_TIMEOUT_S)


def sanitize_label(label: Any) -> str:
    """Labels are echoed into a wake prompt, so strip control characters."""
    text = str(label or "")
    cleaned = "".join(ch for ch in text if ch.isprintable()).strip()
    return cleaned[:MAX_LABEL_CHARS]


# ── log file ──────────────────────────────────────────────────────────────


def trim_log(path: Path) -> None:
    """Keep the tail once the log passes MAX_LOG_BYTES. Never raises."""
    try:
        if not path.exists() or path.stat().st_size <= MAX_LOG_BYTES:
            return
        with path.open("rb") as handle:
            handle.seek(-KEEP_LOG_BYTES, os.SEEK_END)
            kept = handle.read()
        # Drop the partial first line so the tail starts on a boundary.
        newline = kept.find(b"\n")
        if 0 <= newline < len(kept) - 1:
            kept = kept[newline + 1 :]
        path.write_bytes(_TRUNCATION_MARKER + kept)
    except OSError:
        logger.debug("Failed to trim background log %s", path, exc_info=True)


def read_tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    """Last *lines* lines of a run log. Never raises."""
    try:
        if not path.exists():
            return []
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > _TAIL_READ_BYTES:
                handle.seek(-_TAIL_READ_BYTES, os.SEEK_END)
            blob = handle.read()
    except OSError:
        return []
    text = blob.decode("utf-8", errors="replace")
    rows = text.splitlines()
    return rows[-max(1, lines) :]


# ── registry ──────────────────────────────────────────────────────────────


class BackgroundRunStore:
    """JSON-backed storage for background runs (``.runtime/background/``)."""

    def __init__(self, runtime_root: Path) -> None:
        self.root = Path(runtime_root) / STATE_DIR_NAME
        self._path = self.root / STATE_FILE_NAME
        self._lock = threading.RLock()

    def log_path(self, run_id: str) -> Path:
        """Log path for a run.

        Derived from ``run_id`` alone — never from a label, cwd, or anything
        else a caller supplies — and ``run_id`` is minted here, so a log file
        cannot be steered out of the background directory.
        """
        return self.root / f"{run_id}.log"

    def list(self) -> list[BackgroundRun]:
        with self._lock:
            items = [
                self._from_item(item)
                for item in self._load().get("runs", [])
                if isinstance(item, dict)
            ]
        items.sort(key=lambda run: run.started_at)
        return items

    def get(self, run_id: str) -> BackgroundRun | None:
        for run in self.list():
            if run.run_id == run_id:
                return run
        return None

    def replace(self, run: BackgroundRun) -> None:
        with self._lock:
            data = self._load()
            runs = data.setdefault("runs", [])
            for index, item in enumerate(runs):
                if item.get("run_id") == run.run_id:
                    runs[index] = run.to_dict()
                    self._save(data)
                    return
            runs.append(run.to_dict())
            self._save(data)

    def delete(self, run_id: str) -> bool:
        with self._lock:
            data = self._load()
            runs = data.setdefault("runs", [])
            remaining = [item for item in runs if item.get("run_id") != run_id]
            if len(remaining) == len(runs):
                return False
            data["runs"] = remaining
            self._save(data)
            return True

    def _load(self) -> dict[str, Any]:
        if not self._path.exists():
            return {"runs": []}
        try:
            return read_json_dict(self._path)
        except (OSError, json.JSONDecodeError):
            logger.warning("Unreadable background registry at %s; starting empty", self._path)
            return {"runs": []}

    def _save(self, payload: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self._path)

    @staticmethod
    def _from_item(item: dict[str, Any]) -> BackgroundRun:
        known = {f.name for f in BackgroundRun.__dataclass_fields__.values()}
        filtered = {key: value for key, value in item.items() if key in known}
        filtered.setdefault("run_id", "")
        filtered.setdefault("parent_chat_id", "")
        run = BackgroundRun(**filtered)
        if not isinstance(run.cmd, list):
            run.cmd = []
        return run


class BackgroundRunner:
    """Launches, supervises, cancels, and prunes background command runs."""

    def __init__(
        self,
        store: BackgroundRunStore,
        *,
        workspace_root: Path,
        on_finish: Callable[[BackgroundRun, list[str]], None] | None = None,
        record_job_runs: bool = True,
    ) -> None:
        self._store = store
        self._workspace_root = Path(workspace_root)
        self._on_finish = on_finish
        self._record_job_runs = record_job_runs
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._supervisors: dict[str, asyncio.Task[None]] = {}
        self._cancelling: set[str] = set()
        self._janitor: asyncio.Task[None] | None = None
        self._stopping = False

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> list[BackgroundRun]:
        """Resolve restart orphans, prune, and arm the janitor.

        Returns the runs that were resolved as orphans (already woken).
        """
        orphans = self.resolve_orphans()
        self.prune()
        if self._janitor is None:
            self._janitor = asyncio.create_task(self._janitor_loop(), name="background-janitor")
        return orphans

    async def stop(self) -> None:
        """Terminate every live run, then drop the janitor.

        This is what keeps a graceful restart from leaking: by the time uvicorn
        exits, no background child is still running and no registry row is left
        non-terminal.
        """
        self._stopping = True
        if self._janitor is not None:
            self._janitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._janitor
            self._janitor = None
        for run_id in list(self._procs):
            self._cancelling.add(run_id)
            proc = self._procs.get(run_id)
            if proc is not None:
                await self._terminate(proc)
        supervisors = [task for task in self._supervisors.values() if not task.done()]
        if supervisors:
            with contextlib.suppress(Exception):
                await asyncio.wait(supervisors, timeout=CANCEL_GRACE_SECONDS * 2)

    # ── reads ─────────────────────────────────────────────────────────

    def get(self, run_id: str) -> BackgroundRun | None:
        return self._store.get(run_id)

    def list_for_chat(self, chat_id: str) -> list[BackgroundRun]:
        return [run for run in self._store.list() if run.parent_chat_id == chat_id]

    def active_count(self, chat_id: str) -> int:
        return sum(1 for run in self.list_for_chat(chat_id) if not run.is_terminal())

    def log_path(self, run_id: str) -> Path:
        return self._store.log_path(run_id)

    def tail(self, run_id: str, lines: int = TAIL_LINES) -> list[str]:
        return read_tail(self._store.log_path(run_id), lines)

    # ── start ─────────────────────────────────────────────────────────

    async def start_run(
        self,
        *,
        parent_chat_id: str,
        project_id: str = "",
        workspace: str = "",
        cmd: Any,
        cwd: str = "",
        env: Any = None,
        timeout_s: Any = DEFAULT_TIMEOUT_S,
        label: str = "",
    ) -> BackgroundRun:
        if not parent_chat_id:
            raise BackgroundRunError("chat_required", "A background run needs an owning chat.")
        argv = validate_cmd(cmd)
        run_dir = resolve_cwd(self._workspace_root, cwd)
        executable = resolve_executable(argv[0], run_dir, self._workspace_root)
        timeout = validate_timeout(timeout_s)
        clean_label = sanitize_label(label)
        active = self.active_count(parent_chat_id)
        if active >= MAX_ACTIVE_RUNS_PER_CHAT:
            raise BackgroundRunError(
                "run_limit_reached",
                f"{active} background runs are already active (limit "
                f"{MAX_ACTIVE_RUNS_PER_CHAT}). Wait for one to finish or cancel it.",
            )

        run_id = f"bg-{uuid.uuid4().hex[:8]}"
        child_env = build_env(env, run_id=run_id, workspace=workspace)
        log_path = self._store.log_path(run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        header = (
            f"[ciaobot] run {run_id} :: {' '.join(argv)}\n"
            f"[ciaobot] cwd {run_dir}\n"
        )
        log_path.write_text(header, encoding="utf-8")

        run = BackgroundRun(
            run_id=run_id,
            parent_chat_id=parent_chat_id,
            project_id=project_id,
            workspace=workspace,
            label=clean_label,
            cmd=argv,
            cwd=str(run_dir),
            timeout_s=timeout,
            started_at=_now_iso(),
            status="queued",
        )
        self._store.replace(run)

        try:
            proc = await asyncio.create_subprocess_exec(
                executable,
                *argv[1:],
                cwd=str(run_dir),
                env=child_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                # Own session/process group: cancel and timeout can then take
                # down the whole tree the command spawned, not just argv[0].
                start_new_session=True,
            )
        except OSError as exc:
            run.status = "error"
            run.error = f"failed to start: {exc}"
            run.ended_at = _now_iso()
            self._store.replace(run)
            self._finish(run)
            raise BackgroundRunError("start_failed", f"Could not start the command: {exc}") from exc

        run.pid = proc.pid
        run.status = "running"
        self._store.replace(run)
        self._procs[run_id] = proc
        self._supervisors[run_id] = asyncio.create_task(
            self._supervise(run_id, proc, log_path, timeout),
            name=f"background-run-{run_id}",
        )
        return run

    # ── cancel ────────────────────────────────────────────────────────

    async def cancel(self, run_id: str) -> BackgroundRun:
        run = self._store.get(run_id)
        if run is None:
            raise BackgroundRunError("run_not_found", f"Run '{run_id}' was not found.")
        if run.is_terminal():
            return run
        proc = self._procs.get(run_id)
        if proc is None:
            # Non-terminal with no live process: a crash orphan that start()
            # has not resolved yet. Resolve it here rather than hanging.
            return self._resolve_orphan(run)
        self._cancelling.add(run_id)
        await self._terminate(proc)
        supervisor = self._supervisors.get(run_id)
        if supervisor is not None and not supervisor.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    asyncio.shield(supervisor), timeout=CANCEL_GRACE_SECONDS * 2
                )
        return self._store.get(run_id) or run

    # ── supervision ───────────────────────────────────────────────────

    async def _supervise(
        self,
        run_id: str,
        proc: asyncio.subprocess.Process,
        log_path: Path,
        timeout_s: int,
    ) -> None:
        pump: asyncio.Task[None] | None = None
        timed_out = False
        try:
            if proc.stdout is not None:
                pump = asyncio.create_task(
                    self._pump(proc.stdout, log_path), name=f"background-log-{run_id}"
                )
            try:
                exit_code = await asyncio.wait_for(proc.wait(), timeout=timeout_s)
            except TimeoutError:
                timed_out = True
                await self._terminate(proc)
                exit_code = await proc.wait()
            if pump is not None:
                if run_id in self._cancelling:
                    # A killed process group may leave the pipe open briefly
                    # on some runners. Cancellation has already terminated the
                    # command, so do not delay the terminal registry update on
                    # a grandchild that inherited stdout.
                    pump.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await pump
                else:
                    # wait_for cancels the pump on timeout, which is what we
                    # want: a surviving grandchild can hold the pipe open long
                    # after the command itself is done. CancelledError is a
                    # BaseException, so an outside cancel of this supervisor
                    # still propagates.
                    with contextlib.suppress(Exception):
                        await asyncio.wait_for(pump, timeout=CANCEL_GRACE_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — a supervisor crash must still finalize
            logger.exception("Background run %s supervisor failed", run_id)
            self._finalize(run_id, status="error", exit_code=None, error=str(exc)[:500])
            return
        finally:
            self._procs.pop(run_id, None)

        cancelled = run_id in self._cancelling
        self._cancelling.discard(run_id)
        if cancelled:
            reason = "terminated by engine shutdown" if self._stopping else "cancelled"
            self._finalize(run_id, status="cancelled", exit_code=exit_code, error=reason)
        elif timed_out:
            self._finalize(
                run_id,
                status="error",
                exit_code=exit_code,
                error=f"timed out after {timeout_s}s and was terminated",
            )
        elif exit_code == 0:
            self._finalize(run_id, status="ok", exit_code=0, error="")
        else:
            self._finalize(
                run_id, status="error", exit_code=exit_code, error=f"exit {exit_code}"
            )

    async def _pump(self, stream: asyncio.StreamReader, log_path: Path) -> None:
        """Copy the merged stdout/stderr pipe into the rotating log."""
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            try:
                with log_path.open("ab") as handle:
                    handle.write(chunk)
            except OSError:
                logger.debug("Failed writing background log %s", log_path, exc_info=True)
                return
            trim_log(log_path)

    async def _terminate(self, proc: asyncio.subprocess.Process) -> None:
        """SIGTERM the run's process group, then SIGKILL after the grace."""
        if proc.returncode is not None:
            return
        self._signal_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), CANCEL_GRACE_SECONDS)
            return
        except TimeoutError:
            pass
        except Exception:  # noqa: BLE001 — already-reaped or transport churn
            return
        self._signal_group(proc, signal.SIGKILL)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(proc.wait(), CANCEL_GRACE_SECONDS)

    @staticmethod
    def _signal_group(proc: asyncio.subprocess.Process, sig: int) -> None:
        if proc.returncode is not None:
            return
        try:
            # start_new_session made the child a process-group leader, so its
            # pid is the pgid: this reaches the whole tree it spawned.
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            with contextlib.suppress(ProcessLookupError, OSError):
                proc.send_signal(sig)

    # ── finalization ──────────────────────────────────────────────────

    def _finalize(
        self, run_id: str, *, status: str, exit_code: int | None, error: str
    ) -> None:
        run = self._store.get(run_id)
        if run is None:
            return
        run.status = status
        run.exit_code = exit_code
        run.error = error
        run.ended_at = _now_iso()
        self._store.replace(run)
        self._finish(run)

    def _finish(self, run: BackgroundRun) -> None:
        """Record the job run and wake the owning chat. Never raises."""
        self._record(run)
        if self._on_finish is None:
            return
        try:
            self._on_finish(run, self.tail(run.run_id))
        except Exception:  # noqa: BLE001 — a wake failure must not kill the runner
            logger.exception("Background run %s wake failed", run.run_id)

    def _record(self, run: BackgroundRun) -> None:
        if not self._record_job_runs:
            return
        # "cancelled" is not a failure of the command, so it is recorded as
        # skipped rather than painting the Automation page red.
        status = {"ok": "ok", "cancelled": "skipped", "error": "error"}.get(run.status, "error")
        started = _parse_iso_utc(run.started_at)
        ended = _parse_iso_utc(run.ended_at)
        duration_ms = 0
        if started is not None and ended is not None:
            duration_ms = max(0, int((ended - started).total_seconds() * 1000))
        try:
            job_runs.record_run(job_runs.JobRun(
                job="background_run",
                label="Background command run",
                category="system",
                started_at=run.started_at,
                ended_at=run.ended_at,
                duration_ms=duration_ms,
                status=status,
                error=run.error or None,
                extra={
                    "run_id": run.run_id,
                    "run_label": run.label,
                    "cmd": run.cmd,
                    "exit_code": run.exit_code,
                    "chat_id": run.parent_chat_id,
                },
            ))
        except Exception:  # noqa: BLE001 — telemetry must never break a run
            logger.debug("Failed to record background job run", exc_info=True)

    # ── restart orphans and pruning ───────────────────────────────────

    def resolve_orphans(self) -> list[BackgroundRun]:
        """Resolve runs left non-terminal by a crash, and wake their chats.

        Deliberately does not signal the recorded PID: after a crash that PID
        may belong to something else entirely, and there is no dependency-free
        way to prove otherwise. The wake carries the PID and the log path so
        the chat (or the user) can deal with a survivor explicitly.
        """
        resolved: list[BackgroundRun] = []
        for run in self._store.list():
            if run.is_terminal() or run.run_id in self._procs:
                continue
            resolved.append(self._resolve_orphan(run))
        return resolved

    def _resolve_orphan(self, run: BackgroundRun) -> BackgroundRun:
        run.status = "error"
        run.exit_code = None
        run.error = (
            "the engine restarted while this run was active; the process is no "
            "longer tracked"
            + (f" (last known pid {run.pid})" if run.pid else "")
        )
        run.ended_at = _now_iso()
        self._store.replace(run)
        logger.warning("Background run %s orphaned by an engine restart", run.run_id)
        self._finish(run)
        return run

    def prune(self, *, now: datetime | None = None) -> int:
        """Drop terminal runs past the retention window, with their logs."""
        cutoff = (now or _now_utc()) - timedelta(days=RETENTION_DAYS)
        removed = 0
        for run in self._store.list():
            if not run.is_terminal():
                continue
            stamp = _parse_iso_utc(run.ended_at) or _parse_iso_utc(run.started_at)
            if stamp is not None and stamp > cutoff:
                continue
            if self._store.delete(run.run_id):
                removed += 1
            with contextlib.suppress(OSError):
                self._store.log_path(run.run_id).unlink(missing_ok=True)
        return removed

    async def _janitor_loop(self) -> None:
        while True:
            await asyncio.sleep(JANITOR_SECONDS)
            try:
                self.prune()
            except Exception:  # noqa: BLE001
                logger.exception("Background janitor pass failed")
