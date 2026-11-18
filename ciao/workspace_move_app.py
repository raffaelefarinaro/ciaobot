"""The app-mode engine moves its own workspace in the gap of its exec restart.

Ciaobot for Mac has no LaunchAgent and no detached move job: the app spawns
this engine as its child (``--app-engine``) and owns the workspace pointer
(:mod:`ciao.app_engine`), and an engine cannot rename the folder it is
serving while it is serving it. So the move rides the restart the engine
already performs: the running engine (in app mode) records the operation and
exits with its restart code, and the fresh copy of the same pid — before any
configuration is loaded, with no server, threads or locks of its own — runs
:func:`before_boot`:

* the record says ``stopping``: probe the destination, rename the folder,
  rewrite the absolute paths and repoint the pointer, then phase ``verifying``
  so the booted engine can confirm readiness;
* the record says the move was interrupted — the app or the Mac died between
  the rename and readiness, or died mid-step — the very same function rolls the
  move back and boots the old folder. Every rollback runs here. A boot that
  finds ``verifying`` (ready never came) therefore self-heals without an
  operator, and a Quit during a rename is recovered by the supervisor's stop
  grace or the next boot's rollback.

Safety rules, because this renames the user's folder:

* the record is read as strictly as the pointer (own ``0o600`` file, no
  symlink, bounded) and validated before anything moves: absolute resolved
  paths, the source is the pointer's workspace, the folders do not overlap,
  the target is absent or an empty real directory with an existing parent;
* every record write after the rename is best-effort; the pointer follows
  the disk (it names whichever of the two folders holds the data) and is
  repaired from a ``finally``, so a failed record write can never strand the
  data or prevent the rename back;
* never rename while an engine is alive: a held ``server.lock`` skips, and a
  ``verifying`` record younger than ``VERIFY_GRACE_S`` is left alone (an
  engine may still be starting, or has restarted on the new folder ahead of
  ``verify_after_boot``, which writes ``done`` once the booted engine is
  ready — never before — and requests the rollback restart on a timeout).

The channel across boots is the operation record and the pointer. The record
also carries ``app``: a 1.x record written by a launchd move job is never
acted on here, and the LaunchAgent plist is never read or written in app mode.

The request side lives here too: :func:`record` is the only writer of an
app-mode record (the Settings move route; a source-scan test in
``tests/test_workspace_move_app.py`` allows exactly this one module), taking
the move lock it hands to :func:`drain_then_restart`, which drains chats and
scheduled work, writes ``stopping`` and requests the engine's own restart —
the lock's descriptor dies with the ``execv`` and the fresh process's
``before_boot`` takes it for the move. :func:`verify_after_boot`, started by
the booted engine, writes ``done`` once the engine is ready and never before:
without it every ``verifying`` record older than ``VERIFY_GRACE_S`` would be
rolled back by the next ordinary restart, undoing a move that worked, but a
restart that finds ``done`` is just an ordinary restart.
"""

from __future__ import annotations

import asyncio
import errno
import os
import signal
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Awaitable, Callable, Iterator, NamedTuple

from ciao import app_engine, workspace_move
from ciao.config import RESTART_EXIT_CODE
from ciao.os_support.locks import unlock

__all__ = [
    "STALE_STOPPING_S",
    "VERIFY_GRACE_S",
    "before_boot",
    "drain_then_restart",
    "record",
    "verify_after_boot",
]

# A `stopping` record older than this was written by a boot that died before
# it could move anything (drain, or the exec itself): waiting longer cannot
# help. Fresh `stopping` records mean the exec just happened and the move is
# this boot's first duty.
STALE_STOPPING_S = 600.0

# A `verifying` record younger than this is never rolled back: the engine it
# names may still be starting (it holds no lock until it is up), or has
# restarted on the new folder. Longer than the engine's ready timeout.
VERIFY_GRACE_S = workspace_move._READY_TIMEOUT + 60.0

# A record stamped further ahead than this is not trusted: it reads as ancient.
_FUTURE_SKEW_S = 60.0

# How often the booted engine's verify polls readiness.
_VERIFY_POLL_S = 1.0

# How long a boot waits for another boot's move.lock before leaving it alone.
_LOCK_WAIT_S = 20.0

# Phases in which the folder may already have been moved: a boot finding one
# of these (the previous boot died mid-step, or was killed before ready) undoes
# the move instead of continuing it. `verifying` counts: ready never arrived.
_RECOVER_PHASES = frozenset(
    {"moving", "rewriting", "repointing", "verifying", "rolling_back"}
)

_INTERRUPTED_BEFORE_MOVE = (
    "The move was interrupted before the folder was moved; nothing was moved."
)


class _Refused(Exception):
    """The record does not describe a move this boot may perform."""


class _Paths(NamedTuple):
    """The record's folders, absolute and resolved."""

    source: Path
    target: Path
    runtime: Path
    raw_source: str

    def runtime_for(self, side: Path) -> Path:
        """The pointer's runtime root as it reads when the workspace is at ``side``."""
        if side == self.target:
            return Path(
                workspace_move.rewrite_prefix(
                    str(self.runtime), str(self.source), str(self.target)
                )
            )
        return self.runtime

    def source_spellings(self) -> list[str]:
        """The source as resolved, then as the record spelled it when that differs."""
        spellings = [str(self.source)]
        if self.raw_source != spellings[0]:
            spellings.append(self.raw_source)
        return spellings


def before_boot(
    *,
    state_dir: Path | None = None,
    claude_home: Path | None = None,
    now: Callable[[], float] = time.time,
) -> str:
    """Act on the recorded workspace move before the engine boots; report what was done.

    One of ``"none"`` (nothing to do), ``"moved"`` (the rename, the rewrites and
    the pointer repoint succeeded; the record waits at ``verifying``),
    ``"rolled_back"``, ``"rollback_failed"``, ``"failed"`` or ``"skipped"``.
    The engine must boot either way, so this never raises: any exception is
    printed as one line and the boot carries on untouched.
    """
    try:
        return _before_boot(state_dir=state_dir, claude_home=claude_home, now=now)
    except Exception as exc:  # noqa: BLE001 — the boot must survive any move state
        detail = str(exc) or exc.__class__.__name__
        print(f"workspace move: {detail}", file=sys.stderr)
        _emergency_repair(state_dir)
        return "skipped"


def _take_lock(root: Path) -> Any:
    """``move.lock``, waiting a bounded time for another boot's hold to end.

    Raises :class:`workspace_move.MoveError` when it stays held: that boot owns
    the move (and its pointer), and this one touches nothing.
    """
    deadline = time.monotonic() + _LOCK_WAIT_S
    while True:
        try:
            return workspace_move._lock(root)
        except workspace_move.MoveError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def _emergency_repair(state_dir: Path | None) -> None:
    """After an unexpected error: still make the pointer follow the disk.

    Same rules as the main paths: under ``move.lock`` (never racing another
    boot), never under a live engine, signals ignored.
    """
    handle = None
    try:
        root = state_dir or workspace_move.default_state_dir()
        try:
            handle = workspace_move._lock(root)
        except Exception:  # noqa: BLE001 — held by another boot, or no state dir
            return
        op = workspace_move.read_operation_strict(root)
        if op is None or not op.app or op.phase in workspace_move.TERMINAL_PHASES:
            return
        if op.phase not in _RECOVER_PHASES and op.phase != "stopping":
            return
        paths = _validate(op, "recover")
        if _engine_alive(paths):
            return
        errors: list[str] = []
        with _signals_ignored():
            _repair_pointer(paths, errors)
        if errors:
            print(f"workspace move: {'; '.join(errors)}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — best effort on top of best effort
        return
    finally:
        if handle is not None:
            try:
                unlock(handle.fileno())
                handle.close()
            except Exception:  # noqa: BLE001
                pass


def _heal_pointer(paths: _Paths) -> None:
    """Under ``move.lock``: a pointer naming a vanished folder follows the data.

    For the returns that do not move anything (an engine is up, a verify is
    young, the record is unreadable): the pointer must still name a folder that
    exists, or the next boot reads it as no pointer and shows the setup wizard.
    """
    try:
        pointed = app_engine.read_pointer_record()
        if pointed is None or pointed.workspace.is_dir():
            return
        errors: list[str] = []
        with _signals_ignored():
            _repair_pointer(paths, errors)
        if errors:
            print(f"workspace move: {'; '.join(errors)}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — advisory on top of a skip
        print(f"workspace move: {exc}", file=sys.stderr)


def _heal_from_unreadable_record(root: Path) -> None:
    """A record that fails the strict read: use it only to keep the pointer usable.

    The lenient parse names the folders; the pointer must already name one of
    them (the recover validation) and the repair only ever points it at a
    folder that exists and holds the data, so a forged record gains nothing.
    """
    op = workspace_move.read_operation(root)
    if op is None or not op.app or op.phase not in _RECOVER_PHASES:
        return
    try:
        paths = _validate(op, "recover")
    except _Refused:
        return
    if not _engine_alive(paths):
        _heal_pointer(paths)


def _before_boot(
    *,
    state_dir: Path | None,
    claude_home: Path | None,
    now: Callable[[], float],
) -> str:
    from ciao import package_version

    if not package_version.running_from_app_bundle():
        # Only the engine inside Ciaobot.app moves its own folder.
        return "none"
    root = state_dir or workspace_move.default_state_dir()
    # The dir exists for any recorded move (the write created it); a first
    # boot without one still needs somewhere to take the lock.
    from ciao.os_support.private import make_private_dir

    root.mkdir(parents=True, exist_ok=True)
    make_private_dir(root)
    try:
        handle = _take_lock(root)
    except workspace_move.MoveError:
        # Another move (or a drain) holds the lock: touch nothing.
        return "skipped"
    except OSError:
        # No readable state dir: no move was ever recorded in app mode.
        return "none"
    try:
        op = workspace_move.read_operation_strict(root)
        if op is None:
            if (root / workspace_move.OPERATION_NAME).exists():
                print(
                    "workspace move: the move record is not this user's private "
                    "file or is unreadable; ignoring it",
                    file=sys.stderr,
                )
                _heal_from_unreadable_record(root)
            return "none"
        if not op.app or op.phase in workspace_move.TERMINAL_PHASES:
            return "none"
        recover = op.phase in _RECOVER_PHASES
        mode = "forward" if op.phase == "stopping" else "recover" if recover else "light"
        try:
            paths = _validate(op, mode)
        except _Refused as refusal:
            return _refuse(op, root, str(refusal), recover=recover)
        if _engine_alive(paths):
            # Another engine owns one of the folders — an old service-mode
            # engine, or the one that just booted. Never move a folder out
            # from under a live engine.
            if recover:
                _heal_pointer(paths)
            return "skipped"
        if op.phase in ("queued", "draining"):
            # The engine died before the drain finished: nothing moved yet.
            return _fail_without_moving(op, root, _INTERRUPTED_BEFORE_MOVE)
        if op.phase == "stopping":
            if _age(now, op.updated_at) > STALE_STOPPING_S:
                return _fail_without_moving(op, root, _INTERRUPTED_BEFORE_MOVE)
            return _forward(op, root, paths, claude_home)
        if not recover:
            return "none"
        if op.phase == "verifying" and _verifying_age(now, op, root) < VERIFY_GRACE_S:
            # An engine may still be starting on the new folder (it holds no
            # lock yet), or restarted on it before `done` was written. Rolling
            # the folder back under it would be the one unsafe outcome.
            _heal_pointer(paths)
            return "skipped"
        # A recover phase: the previous boot died mid-move, or died on the new
        # folder before it was ready. Undo the move and boot the old folder.
        reason = op.error or (
            "Ciaobot stopped while starting from the new folder"
            if op.phase == "verifying"
            else "Ciaobot stopped in the middle of the move"
        )
        denied = False
        denial = _denial_for(op)
        if denial:
            denied = True
            reason = denial
        return _rollback(op, root, paths, reason, denied=denied)
    finally:
        # Unlike the drain (the exec closes its descriptor), this boot stays
        # in this process: release the lock before the engine boots.
        unlock(handle.fileno())
        handle.close()


# ── validating the record ───────────────────────────────────────────


def _same_folder(a: Path, b: Path) -> bool:
    if a == b:
        return True
    try:
        return a.exists() and b.exists() and os.path.samefile(a, b)
    except OSError:
        return False


def _pointer_workspace() -> Path | None:
    """The workspace the pointer file names, whether or not it is a directory now.

    ``app_engine.read_pointer`` reads a pointer to a vanished folder as no
    pointer, which is exactly the state after the rename; this read keeps the
    same strict file and schema checks but not the directory requirement.
    """
    pointer = app_engine.read_pointer_record()
    return pointer.workspace.resolve() if pointer is not None else None


def _validate(op: workspace_move.MoveOperation, mode: str) -> _Paths:
    """The record's folders, resolved, or :class:`_Refused` with the reason.

    ``mode`` is ``forward`` (nothing has moved: the disk must look untouched),
    ``recover`` (the folder may be at either side) or ``light`` (the checks
    that need no disk state).
    """
    for label, raw in (
        ("source", op.source),
        ("destination", op.target),
        ("runtime root", op.runtime_root),
    ):
        if not raw or not Path(raw).is_absolute():
            raise _Refused(f"The recorded {label} is not an absolute path.")
    try:
        source = Path(op.source).resolve()
        target = Path(op.target).resolve()
        runtime = Path(op.runtime_root).resolve()
    except (OSError, ValueError, RuntimeError) as exc:
        raise _Refused(f"The recorded folders cannot be resolved: {exc}") from exc
    for label, raw in (("source", op.source), ("destination", op.target)):
        if os.path.islink(raw):
            raise _Refused(f"The recorded {label} is a symbolic link.")
    same = _same_folder(source, target)
    case_only = (
        same
        and source != target
        and source.parent == target.parent
        and source.name.casefold() == target.name.casefold()
    )
    if same and not case_only:
        raise _Refused("The source and the destination are the same folder.")
    if not same and (
        workspace_move._is_within(target, source) or workspace_move._is_within(source, target)
    ):
        raise _Refused("The source and the destination are inside one another.")
    paths = _Paths(source, target, runtime, op.source)
    if mode == "light":
        return paths
    pointed = _pointer_workspace()
    if mode == "forward":
        if pointed is None or not _same_folder(pointed, source):
            raise _Refused("The recorded source is not the workspace Ciaobot is using.")
        if not source.is_dir():
            raise _Refused(f"The source {source} is not a folder.")
        if not target.parent.is_dir():
            raise _Refused(f"The folder {target.parent} does not exist.")
        if not case_only and os.path.lexists(target):
            if not target.is_dir() or any(target.iterdir()):
                raise _Refused(f"The destination {target} already exists and is not empty.")
    elif pointed is not None and not (
        _same_folder(pointed, source) or _same_folder(pointed, target)
    ):
        raise _Refused("The recorded folders are not the workspace Ciaobot is using.")
    return paths


def _engine_alive(paths: _Paths) -> bool:
    """Whether an engine holds ``server.lock`` in either folder or runtime root."""
    if workspace_move._server_lock_held(paths.source) or workspace_move._server_lock_held(
        paths.target
    ):
        return True
    return any(
        workspace_move._runtime_lock_held(runtime)
        for runtime in {paths.runtime, paths.runtime_for(paths.target)}
    )


def _refuse(
    op: workspace_move.MoveOperation, root: Path, reason: str, *, recover: bool
) -> str:
    """Record a refused record and do nothing else: no rename, no pointer write."""
    message = f"The move was refused: {reason}"
    if recover:
        _advance_quiet(op, root, "rollback_failed", message)
        return "rollback_failed"
    _advance_quiet(op, root, "failed", message)
    return "failed"


# ── the record ──────────────────────────────────────────────────────


def _fail_without_moving(
    op: workspace_move.MoveOperation, root: Path, message: str
) -> str:
    _advance_quiet(op, root, "failed", message)
    return "failed"


def _age(now: Callable[[], float], stamp: str) -> float:
    """Seconds since ``stamp``; unparsable or future-dated stamps read as ancient."""
    try:
        age = now() - datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return float("inf")
    if age < -_FUTURE_SKEW_S:
        return float("inf")
    return max(age, 0.0)


def _verifying_age(
    now: Callable[[], float], op: workspace_move.MoveOperation, root: Path
) -> float:
    """Seconds since a ``verifying`` record was written, erring toward young.

    The reverse of :func:`_age`: rolling back under a live engine is the unsafe
    outcome, so a stamp that cannot be trusted (empty, unparsable, in the
    future, or the clock stepped backward) is young, never ancient. The record
    file's own mtime bounds the stamp: whichever says the record is newer wins.
    """
    try:
        stamp_age = now() - datetime.fromisoformat(op.updated_at).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return 0.0
    if stamp_age < 0:
        return 0.0
    try:
        file_age = now() - (root / workspace_move.OPERATION_NAME).stat().st_mtime
    except OSError:
        return stamp_age
    if file_age < 0:
        return 0.0
    return min(stamp_age, file_age)


def _denial_for(op: workspace_move.MoveOperation) -> str:
    """The recorded macOS denial, only when it was recorded during ``op``.

    The denial file is shared and has no operation id; one written before this
    operation started is somebody else's, and must not replace the reason this
    rollback has.
    """
    from ciao import workspace_access

    text = workspace_access.read_denial()
    if not text:
        return ""
    try:
        started = datetime.fromisoformat(op.started_at).timestamp()
        recorded = workspace_access.denial_path().stat().st_mtime
    except (TypeError, ValueError, OverflowError, OSError):
        return ""
    return text if recorded >= started else ""


def _advance(
    op: workspace_move.MoveOperation, root: Path, phase: str, error: str = ""
) -> None:
    op.phase = phase
    op.error = error
    op.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    workspace_move.write_operation(op, root)


def _advance_quiet(
    op: workspace_move.MoveOperation, root: Path, phase: str, error: str = ""
) -> bool:
    """:func:`_advance` that never raises: the record is advisory, the disk is not."""
    try:
        _advance(op, root, phase, error)
    except Exception as exc:  # noqa: BLE001 — a failed write must not stop recovery
        print(
            f"workspace move: could not record phase {phase}: "
            f"{str(exc) or exc.__class__.__name__}",
            file=sys.stderr,
        )
        return False
    return True


# ── forward ─────────────────────────────────────────────────────────


def _forward(
    op: workspace_move.MoveOperation,
    root: Path,
    paths: _Paths,
    claude_home: Path | None,
) -> str:
    """Perform ``op``: probe, rename, rewrite and repoint the pointer.

    Signals are ignored across the whole move (see :func:`_signals_ignored`):
    a Quit that lands between the rename and the pointer repoint would
    otherwise leave the workspace half-moved, and the next boot would read a
    pointer that does not match the disk.
    """
    source, target = paths.source, paths.target
    with _signals_ignored():
        try:
            # Before anything moves: a folder macOS keeps Ciaobot out of is a
            # first-class denial (the record names it, phase ``failed``), never
            # a half-moved workspace.
            workspace_move._probe_destination_access(target)
        except Exception as exc:  # noqa: BLE001 — recorded for the operator
            reason = str(exc) or exc.__class__.__name__
            if workspace_move._is_privacy_denial(exc):
                op.denied = True
                reason = workspace_move._record_denied_folder(target, app=True)
            _advance_quiet(op, root, "failed", reason)
            return "failed"

        moved = False
        try:
            _advance(op, root, "moving")
            # The destination was validated absent or an empty directory;
            # rename replaces an empty directory and refuses a full one, so
            # nothing is removed beforehand.
            os.rename(source, target)
            moved = True
            _advance(op, root, "rewriting")
            for old in paths.source_spellings():
                workspace_move.rewrite_workspace_state(target, old, str(target))
            workspace_move.copy_claude_sessions(source, target, home=claude_home)
            for old in paths.source_spellings():
                workspace_move.rekey_claude_json(old, str(target), home=claude_home)
            _advance(op, root, "repointing")
            # The pointer follows the disk: it is written only after the
            # rename and every rewrite have succeeded, so the boot that next
            # reads it (this one, seconds later) finds the folder it names.
            app_engine.write_pointer(target, paths.runtime_for(target))
            _advance(op, root, "verifying")
            # The booted engine's verify marks ``done`` (or rolls back).
            return "moved"
        except Exception as exc:  # noqa: BLE001 — every failure after the rename undoes it
            reason = str(exc) or exc.__class__.__name__
            if not moved:
                if isinstance(exc, OSError) and exc.errno == errno.EXDEV:
                    reason = (
                        f"{target} is on a different volume than {source}; "
                        "the workspace is not copied across volumes, so nothing was moved."
                    )
                _advance_quiet(op, root, "failed", reason)
                return "failed"
            # Nothing here may raise before the rollback runs: the folder is
            # renamed and only the rollback puts it back.
            try:
                if workspace_move._is_privacy_denial(exc):
                    op.denied = True
                    reason = workspace_move._record_denied_folder(
                        workspace_move._denied_folder(source, target), app=True
                    )
            except Exception as describe_exc:  # noqa: BLE001
                reason = f"{reason}; recording the denial failed: {describe_exc}"
            return _rollback(op, root, paths, reason)


# ── rollback ────────────────────────────────────────────────────────


def _populated(folder: Path) -> bool:
    """Whether ``folder`` is a directory holding anything (unreadable counts as yes)."""
    try:
        return folder.is_dir() and any(folder.iterdir())
    except OSError:
        return True


def _exact(path: Path) -> bool:
    """Whether ``path``'s final component exists with exactly this spelling."""
    try:
        return path.name in os.listdir(path.parent)
    except OSError:
        return False


def _undo_rename(paths: _Paths, errors: list[str]) -> None:
    source, target = paths.source, paths.target
    if not os.path.lexists(target) and source.is_dir() and not os.path.islink(source):
        # The folder is already back: a boot that died after the rename back,
        # before the paths were rewritten (or the move never got that far).
        # The rewrite is idempotent, so finish it.
        _rewrite_back(paths, errors)
        return
    if not target.is_dir() or os.path.islink(target):
        return
    if source.exists() and _same_folder(source, target):
        # One folder under two spellings (a case-only rename on a
        # case-insensitive volume): restore the source's spelling.
        if not (_exact(target) and not _exact(source)):
            return
    elif not _populated(target):
        return
    elif source.exists() and (not source.is_dir() or _populated(source)):
        errors.append(
            "both the source and the destination hold files; nothing was moved back"
        )
        return
    try:
        os.rename(target, source)
    except Exception as exc:  # noqa: BLE001 — every step runs, errors are collected
        errors.append(f"moving the folder back: {exc}")
        return
    _rewrite_back(paths, errors)


def _rewrite_back(paths: _Paths, errors: list[str]) -> None:
    """Rewrite the target's paths to the source's. Both spellings of the source
    were rewritten to the one target spelling going forward, so they come back
    as the one resolved source spelling (the same folder)."""
    try:
        workspace_move.rewrite_workspace_state(
            paths.source, str(paths.target), str(paths.source)
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"rewriting the folder's paths back: {exc}")


def _data_side(paths: _Paths) -> Path | None:
    """The folder the pointer should name: the one that exists and holds the data."""
    source, target = paths.source, paths.target
    usable = [
        side for side in (source, target) if side.is_dir() and not os.path.islink(side)
    ]
    if not usable:
        return None
    if len(usable) == 2 and _same_folder(source, target):
        return source
    populated = [side for side in usable if _populated(side)]
    if len(populated) == 1:
        return populated[0]
    if len(populated) == 2:
        return None  # both hold data: not ours to choose
    return usable[0]  # neither holds data; the source is listed first


def _repair_pointer(paths: _Paths, errors: list[str]) -> None:
    """Point the pointer at whichever folder exists and holds the data."""
    try:
        side = _data_side(paths)
        if side is None:
            errors.append(
                "no single folder holds the workspace; the pointer was left as it was"
            )
            return
        app_engine.write_pointer(side, paths.runtime_for(side))
    except Exception as exc:  # noqa: BLE001 — collected like every other step
        errors.append(f"writing the workspace pointer: {exc}")


def _rollback(
    op: workspace_move.MoveOperation,
    state_dir: Path,
    paths: _Paths,
    reason: str,
    *,
    denied: bool = False,
) -> str:
    """Undo ``op``: rename the folder back, rewrite the paths back, repoint.

    Every rollback runs here — the app quit mid-move, the new folder never
    came ready, or a rewrite or the repoint itself failed. Record writes are
    best-effort and never gate a step: the rename back and the pointer repair
    run even when the record cannot be written. The pointer follows the disk
    (see :func:`_data_side`), so the engine always boots a real workspace and
    shows ``rollback_failed``, never the setup wizard.
    """
    with _signals_ignored():
        op.denied = op.denied or denied
        _advance_quiet(op, state_dir, "rolling_back", reason)
        errors: list[str] = []
        try:
            _undo_rename(paths, errors)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"moving the folder back: {exc}")
        finally:
            _repair_pointer(paths, errors)
        if errors:
            _advance_quiet(
                op, state_dir, "rollback_failed", f"{reason}; rollback: {'; '.join(errors)}"
            )
            return "rollback_failed"
        _advance_quiet(op, state_dir, "rolled_back", reason)
        return "rolled_back"


@contextmanager
def _signals_ignored() -> Iterator[None]:
    """SIGTERM, SIGINT and SIGHUP are ignored inside the block, restored after.

    While the folder is being renamed or rewritten, a Quit (or a hung-up
    terminal during a manual run) must not end the process half-moved: the
    supervisor's stop grace rides out the seconds this takes, and a kill that
    lands anyway is undone by the next boot's rollback. Same shape
    :func:`ciao.workspace_move.spawn_job` uses for the launchd move job; this
    module never execs, so no ignored disposition leaks past the block.
    Handlers can only be installed from the main thread; elsewhere the block
    runs unprotected rather than crashing the boot. A disposition Python did
    not install (``getsignal`` returns ``None``) is restored as the default.
    """
    previous: list[tuple[signal.Signals, Any]] = []
    targets: list[signal.Signals] = [signal.SIGTERM, signal.SIGINT]
    sighup = getattr(signal, "SIGHUP", None)  # not on win32
    if sighup is not None:
        targets.append(sighup)
    for sig in targets:
        try:
            before = signal.getsignal(sig)
            signal.signal(sig, signal.SIG_IGN)
        except (ValueError, OSError):
            continue  # not the main thread: nothing is installed, nothing to restore
        previous.append((sig, signal.SIG_DFL if before is None else before))
    try:
        yield
    finally:
        for sig, handler in previous:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


# ── the request side: record the move, drain, restart, verify ────────


#: Lock handles the drain deliberately keeps open across the restart: the
#: ``execv`` that ends the engine is what closes the descriptor (Python fds
#: are close-on-exec), and :func:`before_boot` in the fresh process takes the
#: lock itself. Without a live reference, the drain coroutine ending would let
#: the file object be collected and the lock released while the engine is
#: still winding down to re-exec.
_HELD_LOCKS: list[IO[str]] = []


def _release(handle: IO[str]) -> None:
    """Unlock and close ``handle``; the record of the failure is already written."""
    try:
        unlock(handle.fileno())
        handle.close()
    except OSError:
        pass


def record(
    move_plan: workspace_move.MovePlan, *, port: int, state_dir: Path | None = None
) -> tuple[workspace_move.MoveOperation, IO[str]]:
    """Record the app-mode move and hold its lock until the exec closes it.

    Called off the event loop by the move route: taking the lock and writing
    the record block. Raises :class:`workspace_move.MoveError` when the plan
    is not ``ok`` — nothing is written, nothing taken — or when another move
    already holds the lock. On success the record is ``queued`` with
    ``app`` set (the strict private writer, like the pointer), and the lock
    stays open: the caller hands it to :func:`drain_then_restart`.
    """
    if not move_plan.ok:
        raise workspace_move.MoveError(" ".join(move_plan.refusals))
    root = state_dir or workspace_move.default_state_dir()
    from ciao.os_support.private import make_private_dir

    root.mkdir(parents=True, exist_ok=True)
    make_private_dir(root)
    handle = workspace_move._lock(root)
    try:
        pointer = app_engine.read_pointer()
        # The pointer's runtime root before the move: the rollback and the
        # forward rewrite both need it spelled for the old folder.
        runtime_root = str(pointer.runtime_root) if pointer is not None else ""
        stamp = workspace_move._now()
        op = workspace_move.MoveOperation(
            id=f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}",
            phase="queued",
            source=move_plan.source,
            target=move_plan.target,
            started_at=stamp,
            updated_at=stamp,
            port=port,
            python=sys.executable,
            app=True,
            runtime_root=runtime_root,
        )
        workspace_move.write_operation(op, root)
    except BaseException:
        # The lock must not outlive this call on a failure: it was taken to
        # hand a live record to the drain, and a failed record hands nothing.
        _release(handle)
        raise
    return op, handle


async def drain_then_restart(
    op: workspace_move.MoveOperation,
    lock_handle: IO[str],
    *,
    pcm: Any,
    schedules: Any,
    request_restart: Callable[[int], None],
    state_dir: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Drain chats and scheduled work, record ``stopping``, restart the engine.

    Runs as a task on the live engine, spawned by the move route after
    :func:`record`. Admission closes under this drain's owner
    (``server_restarting`` reaches the PWA as the restart overlay), work is
    polled until it has been idle for ``_IDLE_POLLS_REQUIRED`` consecutive
    polls or ``_DRAIN_TIMEOUT`` passes, and only then is ``stopping`` written
    and the engine's own restart requested: ``request_restart`` drains the
    rest, the process re-execs with the same pid, and ``before_boot`` — the
    fresh process's first duty — takes the lock this drain deliberately keeps
    open and moves the folder.

    Every failure reopens admission, records ``failed`` (nothing was moved)
    and releases the lock. On success the lock is never released here: the
    ``execv`` closes the descriptor.
    """
    root = state_dir or workspace_move.default_state_dir()
    owner = f"move-{op.id}"

    if getattr(pcm, "restart_draining", False):
        # A Settings restart or an update is already draining: starting a move
        # on top would wait for work that that drain is about to kill, and
        # both would restart the engine as rivals.
        _advance_quiet(
            op, root, "failed", "Ciaobot is already restarting. Try again when it is back."
        )
        _release(lock_handle)
        return
    if schedules is None:
        # Refuse before admission closes: whether scheduled work is idle is
        # unanswerable without a schedule manager, and
        # `workspace_move._schedule_work_idle` owns the operator copy for that
        # answer (an empty body has no count — exactly what None cannot give).
        _release(lock_handle)
        workspace_move._schedule_work_idle({})
        return
    try:
        _advance_quiet(op, root, "draining")
        pcm.begin_restart_drain(owner)
        idle = 0
        deadline = clock() + workspace_move._DRAIN_TIMEOUT
        while True:
            idle = (
                0
                if pcm.active_chat_ids() or schedules.work_count()
                else idle + 1
            )
            if idle >= workspace_move._IDLE_POLLS_REQUIRED:
                break
            if clock() >= deadline:
                pcm.cancel_restart_drain(owner)
                _advance_quiet(
                    op,
                    root,
                    "failed",
                    f"chats were still running after {int(workspace_move._DRAIN_TIMEOUT)}s; "
                    "nothing was moved",
                )
                _release(lock_handle)
                return
            await sleep(workspace_move._POLL_INTERVAL)
        # Written before the restart it names: the exec is what follows, and
        # the fresh boot's first duty is to act on exactly this record. A write
        # that fails leaves the record in `draining`/`queued`, which
        # `before_boot` refuses to move on — no record, no move.
        _advance_quiet(op, root, "stopping")
        # Deliberately not released: the exec closes the descriptor, and the
        # fresh process's before_boot takes the lock for the move. The holder
        # keeps the fd open until then.
        _HELD_LOCKS.append(lock_handle)
        request_restart(RESTART_EXIT_CODE)
    except asyncio.CancelledError:
        # The engine is shutting down (an app quit, or a server restart
        # cancelled this task): reopen admission, mark the move failed —
        # nothing moved — and let the cancellation finish.
        pcm.cancel_restart_drain(owner)
        _advance_quiet(
            op, root, "failed", "the move was cancelled while draining; nothing was moved"
        )
        _release(lock_handle)
        raise
    except Exception as exc:  # noqa: BLE001 — a drain failure must reopen admission
        pcm.cancel_restart_drain(owner)
        _advance_quiet(
            op, root, "failed", f"{str(exc) or exc.__class__.__name__}; nothing was moved"
        )
        _release(lock_handle)


async def verify_after_boot(
    workspace_root: Path | str,
    tracker: Any,
    request_restart: Callable[[int], None],
    *,
    state_dir: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """Confirm the moved folder became ready; mark ``done``, or undo the move.

    Started by the booted engine (``main``) as a task. The record must be this
    app's ``verifying`` move for the folder this engine is serving; anything
    else is not this boot's duty. ``done`` is written only once the engine
    really is ready — never before, so a restart that finds ``done`` can be an
    ordinary restart, and a ``verifying`` record an engine left behind by
    dying is still the next boot's signal to undo the move. On a timeout the
    record says ``rolling_back`` and the restart is requested whose pre-boot
    pass rolls the move back.
    """
    from ciao import package_version

    if not package_version.running_from_app_bundle():
        return
    root = state_dir or workspace_move.default_state_dir()
    op = workspace_move.read_operation_strict(root)
    if (
        op is None
        or not op.app
        or op.phase != "verifying"
        or Path(op.target).resolve() != Path(workspace_root).resolve()
    ):
        return
    deadline = clock() + workspace_move._READY_TIMEOUT
    ready = bool(tracker.overall_ready)
    while not ready and clock() < deadline:
        await sleep(_VERIFY_POLL_S)
        ready = bool(tracker.overall_ready)
    if ready:
        _advance_quiet(op, root, "done")
        return
    _advance_quiet(
        op,
        root,
        "rolling_back",
        f"the engine did not become ready within {int(workspace_move._READY_TIMEOUT)}s",
    )
    request_restart(RESTART_EXIT_CODE)
