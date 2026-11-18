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

The channel across boots is the operation record and the pointer. The record
also carries ``app``: a 1.x record written by a launchd move job is never
acted on here, and the LaunchAgent plist is never read or written in app mode.
"""

from __future__ import annotations

import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from ciao import app_engine, workspace_move
from ciao.os_support.locks import unlock

__all__ = ["before_boot", "STALE_STOPPING_S"]

# A `stopping` record older than this was written by a boot that died before
# it could move anything (drain, or the exec itself): waiting longer cannot
# help. Fresh `stopping` records mean the exec just happened and the move is
# this boot's first duty.
STALE_STOPPING_S = 600.0

# Phases in which the folder may already have been moved: a boot finding one
# of these (the previous boot died mid-step, or was killed before ready) undoes
# the move instead of continuing it. `verifying` counts: ready never arrived.
_RECOVER_PHASES = frozenset(
    {"moving", "rewriting", "repointing", "verifying", "rolling_back"}
)

_INTERRUPTED_BEFORE_MOVE = (
    "The move was interrupted before the folder was moved; nothing was moved."
)


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
        return "skipped"


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
        handle = workspace_move._lock(root)
    except workspace_move.MoveError:
        # Another move (or a drain) holds the lock: touch nothing.
        return "skipped"
    except OSError:
        # No readable state dir: no move was ever recorded in app mode.
        return "none"
    try:
        op = workspace_move.read_operation(root)
        if op is None or not op.app or op.phase in workspace_move.TERMINAL_PHASES:
            return "none"
        if workspace_move._server_lock_held(Path(op.source)) or workspace_move._server_lock_held(
            Path(op.target)
        ):
            # Another engine owns one of the folders — an old service-mode
            # engine, say. Never move a folder out from under a live engine.
            return "skipped"
        if op.phase in ("queued", "draining"):
            # The engine died before the drain finished: nothing moved yet.
            return _fail_without_moving(op, root, _INTERRUPTED_BEFORE_MOVE)
        if op.phase == "stopping":
            if _age(now, op.updated_at) > STALE_STOPPING_S:
                return _fail_without_moving(op, root, _INTERRUPTED_BEFORE_MOVE)
            return _forward(op, root, claude_home)
        if op.phase not in _RECOVER_PHASES:
            return "none"
        # A recover phase: the previous boot died mid-move, or died on the new
        # folder before it was ready. Undo the move and boot the old folder.
        reason = op.error or (
            "Ciaobot stopped while starting from the new folder"
            if op.phase == "verifying"
            else "Ciaobot stopped in the middle of the move"
        )
        denied = False
        from ciao import workspace_access

        denial = workspace_access.read_denial()
        if denial:
            denied = True
            reason = denial
        return _rollback(op, root, reason, denied=denied)
    finally:
        # Unlike the drain (the exec closes its descriptor), this boot stays
        # in this process: release the lock before the engine boots.
        unlock(handle.fileno())
        handle.close()


def _fail_without_moving(
    op: workspace_move.MoveOperation, root: Path, message: str
) -> str:
    _advance(op, root, "failed", message)
    return "failed"


def _age(now: Callable[[], float], stamp: str) -> float:
    try:
        return now() - datetime.fromisoformat(stamp).timestamp()
    except (TypeError, ValueError):
        return float("inf")


def _advance(
    op: workspace_move.MoveOperation, root: Path, phase: str, error: str = ""
) -> None:
    op.phase = phase
    op.error = error
    op.updated_at = datetime.now(UTC).isoformat(timespec="seconds")
    workspace_move.write_operation(op, root)


def _forward(
    op: workspace_move.MoveOperation, root: Path, claude_home: Path | None
) -> str:
    """Perform ``op``: probe, rename, rewrite and repoint the pointer.

    Signals are ignored across the whole move (see :func:`_signals_ignored`):
    a Quit that lands between the rename and the pointer repoint would
    otherwise leave the workspace half-moved, and the next boot would read a
    pointer that does not match the disk.
    """
    source, target = Path(op.source), Path(op.target)
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
            _advance(op, root, "failed", reason)
            return "failed"

        moved = False
        try:
            _advance(op, root, "moving")
            # An empty target left by an earlier attempt is not a refusal.
            if target.is_dir() and not any(target.iterdir()):
                target.rmdir()
            os.rename(source, target)
            moved = True
            _advance(op, root, "rewriting")
            workspace_move.rewrite_workspace_state(target, str(source), str(target))
            workspace_move.copy_claude_sessions(source, target, home=claude_home)
            workspace_move.rekey_claude_json(str(source), str(target), home=claude_home)
            _advance(op, root, "repointing")
            # The pointer follows the disk: it is written only after the
            # rename and every rewrite have succeeded, so the boot that next
            # reads it (this one, seconds later) finds the folder it names.
            app_engine.write_pointer(
                target,
                Path(
                    workspace_move.rewrite_prefix(op.runtime_root, str(source), str(target))
                ),
            )
            _advance(op, root, "verifying")
            # The booted engine's verify marks ``done`` (or rolls back).
            return "moved"
        except Exception as exc:  # noqa: BLE001 — every failure after the rename undoes it
            reason = str(exc) or exc.__class__.__name__
            if not moved:
                _advance(op, root, "failed", reason)
                return "failed"
            if workspace_move._is_privacy_denial(exc):
                op.denied = True
                reason = workspace_move._record_denied_folder(
                    workspace_move._denied_folder(source, target), app=True
                )
            return _rollback(op, root, reason)


def _rollback(
    op: workspace_move.MoveOperation,
    state_dir: Path,
    reason: str,
    *,
    denied: bool = False,
) -> str:
    """Undo ``op``: rename the folder back, rewrite the paths back, repoint.

    Every rollback runs here — the app quit mid-move, the new folder never
    came ready, or a rewrite or the repoint itself failed. The pointer follows
    the disk: it names whichever of the source and target is a directory,
    preferring the source, so the engine always boots a real workspace and
    shows ``rollback_failed``, never the setup wizard.
    """
    source, target = Path(op.source), Path(op.target)
    with _signals_ignored():
        op.denied = op.denied or denied
        _advance(op, state_dir, "rolling_back", reason)
        errors: list[str] = []
        if target.is_dir() and not source.exists():
            try:
                os.rename(target, source)
            except Exception as exc:  # noqa: BLE001 — every step runs, errors are collected
                errors.append(f"moving the folder back: {exc}")
            else:
                try:
                    workspace_move.rewrite_workspace_state(
                        source, str(target), str(source)
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"rewriting the folder's paths back: {exc}")
        # The pointer follows whatever folder actually exists now.
        try:
            if source.is_dir():
                app_engine.write_pointer(source, Path(op.runtime_root))
            elif target.is_dir():
                app_engine.write_pointer(
                    target,
                    Path(
                        workspace_move.rewrite_prefix(
                            op.runtime_root, str(source), str(target)
                        )
                    ),
                )
        except Exception as exc:  # noqa: BLE001 — collected like every other step
            errors.append(f"writing the workspace pointer: {exc}")
        if errors:
            _advance(
                op, state_dir, "rollback_failed", f"{reason}; rollback: {'; '.join(errors)}"
            )
            return "rollback_failed"
        _advance(op, state_dir, "rolled_back", reason)
        return "rolled_back"


@contextmanager
def _signals_ignored() -> Any:
    """SIGTERM, SIGINT and SIGHUP are ignored inside the block, restored after.

    While the folder is being renamed or rewritten, a Quit (or a hung-up
    terminal during a manual run) must not end the process half-moved: the
    supervisor's stop grace rides out the seconds this takes, and a kill that
    lands anyway is undone by the next boot's rollback. Same shape
    :func:`ciao.workspace_move.spawn_job` uses for the launchd move job; this
    module never execs, so no ignored disposition leaks past the block.
    """
    previous: list[tuple[signal.Signals, Any]] = []
    targets: list[signal.Signals] = [signal.SIGTERM, signal.SIGINT]
    sighup = getattr(signal, "SIGHUP", None)  # not on win32
    if sighup is not None:
        targets.append(sighup)
    try:
        for sig in targets:
            previous.append((sig, signal.getsignal(sig)))
            signal.signal(sig, signal.SIG_IGN)
        yield
    finally:
        for sig, handler in previous:
            signal.signal(sig, handler)
