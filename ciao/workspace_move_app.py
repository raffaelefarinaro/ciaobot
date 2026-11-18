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
  ``verifying`` record younger than ``VERIFY_GRACE_S`` is left alone (an engine
  may still be starting, or may have restarted on the new folder). S3b-2's
  ``verify_after_boot`` writes ``done`` once the booted engine is ready; until
  it lands, a ``verifying`` record older than the grace is rolled back by the
  next boot.

The channel across boots is the operation record and the pointer. The record
also carries ``app``: a 1.x record written by a launchd move job is never
acted on here, and the LaunchAgent plist is never read or written in app mode.
"""

from __future__ import annotations

import errno
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator, NamedTuple

from ciao import app_engine, workspace_move
from ciao.os_support.locks import unlock

__all__ = ["before_boot", "STALE_STOPPING_S", "VERIFY_GRACE_S"]

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


def _emergency_repair(state_dir: Path | None) -> None:
    """After an unexpected error: still make the pointer follow the disk."""
    try:
        root = state_dir or workspace_move.default_state_dir()
        op = workspace_move.read_operation_strict(root)
        if op is None or not op.app or op.phase in workspace_move.TERMINAL_PHASES:
            return
        if op.phase not in _RECOVER_PHASES and op.phase != "stopping":
            return
        paths = _validate(op, "recover")
        errors: list[str] = []
        _repair_pointer(paths, errors)
        if errors:
            print(f"workspace move: {'; '.join(errors)}", file=sys.stderr)
    except Exception:  # noqa: BLE001 — best effort on top of best effort
        return


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
        op = workspace_move.read_operation_strict(root)
        if op is None:
            if (root / workspace_move.OPERATION_NAME).exists():
                print(
                    "workspace move: the move record is not this user's private "
                    "file or is unreadable; ignoring it",
                    file=sys.stderr,
                )
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
        if op.phase == "verifying" and _age(now, op.updated_at) < VERIFY_GRACE_S:
            # An engine may still be starting on the new folder (it holds no
            # lock yet), or restarted on it before `done` was written. Rolling
            # the folder back under it would be the one unsafe outcome.
            return "skipped"
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
    same strict file checks but not the directory requirement.
    """
    raw = app_engine._read_private_json(
        app_engine.state_dir() / app_engine.WORKSPACE_POINTER_NAME,
        label="the app engine workspace pointer",
    )
    if raw is None:
        return None
    schema = raw.get("schema")
    if isinstance(schema, bool) or schema != app_engine.SCHEMA:
        return None
    workspace = raw.get("workspace")
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        return None
    return Path(workspace).resolve()


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
            if workspace_move._is_privacy_denial(exc):
                op.denied = True
                reason = workspace_move._record_denied_folder(
                    workspace_move._denied_folder(source, target), app=True
                )
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
    try:
        workspace_move.rewrite_workspace_state(source, str(target), str(source))
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
