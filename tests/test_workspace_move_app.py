"""`ciao.workspace_move_app`: the app-mode engine's pre-boot move and rollback.

The engine inside Ciaobot for Mac has no LaunchAgent and no detached move job:
the move rides its own exec restart. These tests build real workspace folders
(as the pytest conftest's isolated home: the pointer is the app-owned record
under ``~/Library/Application Support/Ciaobot/engine``), write the operation
record and drive :func:`ciao.workspace_move_app.before_boot` directly. Nothing
here touches the operator's real home, real LaunchAgents or live install.
"""

from __future__ import annotations

import errno
import json
import os
import plistlib
import signal
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

import pytest

from ciao import app_engine, workspace_move, workspace_move_app
from ciao.config import reset_exported_dotenv


def _workspace(root: Path) -> Path:
    """A real workspace folder: `.env` and a `.runtime/state.json` naming it."""
    (root / ".runtime").mkdir(parents=True)
    (root / ".runtime" / "server.lock").touch()
    (root / ".env").write_text(
        f"PWA_PORT=8443\nCIAO_RUNTIME_ROOT={root / '.runtime'}\n",
        encoding="utf-8",
    )
    (root / ".runtime" / "state.json").write_text(
        json.dumps({"workspace": str(root), "vault": str(root / "vault")}),
        encoding="utf-8",
    )
    return root


def _app_mode(
    monkeypatch: pytest.MonkeyPatch, bundled: bool = True
) -> None:
    """Act as if this engine runs from Ciaobot.app's Resources/engine."""
    monkeypatch.setattr(
        "ciao.package_version.running_from_app_bundle", lambda: bundled
    )


def _op(
    source: Path,
    target: Path,
    *,
    phase: str = "stopping",
    app: bool = True,
    runtime_root: str = "",
    error: str = "",
    updated_ago: float = 0.0,
) -> workspace_move.MoveOperation:
    """The operation record the app-mode flow leaves behind for this boot."""
    stamp = (datetime.now(UTC) - timedelta(seconds=updated_ago)).isoformat(
        timespec="seconds"
    )
    return workspace_move.MoveOperation(
        id="op-1",
        phase=phase,
        source=str(source),
        target=str(target),
        started_at=stamp,
        updated_at=stamp,
        error=error,
        app=app,
        runtime_root=runtime_root,
    )


def _store(
    root: Path, op: workspace_move.MoveOperation
) -> Path:
    """Record ``op`` in ``root``; returns ``root`` so tests can chain it."""
    workspace_move.write_operation(op, root)
    # The record's mtime tracks its stamp (a record written `updated_ago`
    # seconds ago was written then), as it would on a real disk.
    try:
        stamp = datetime.fromisoformat(op.updated_at).timestamp()
    except ValueError:
        stamp = time.time()
    os.utime(root / workspace_move.OPERATION_NAME, (stamp, stamp))
    return root


def _state_dir(root: Path) -> workspace_move.MoveOperation | None:
    return workspace_move.read_operation(root)


# ── the app-owned state dir, workspace and port ─────────────────────


def test_default_state_dir_is_app_owned_in_app_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _app_mode(monkeypatch)
    assert workspace_move.default_state_dir() == (
        app_engine.state_dir() / "workspace-move"
    )


def test_registered_workspace_reads_pointer_not_plist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    home = Path.home()
    source = _workspace(tmp_path / "ws")
    app_engine.write_pointer(source, source / ".runtime")

    agents = home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    # A 1.x install's plist still naming its own workspace: in app mode it is
    # never read, so the pointer wins even with this present.
    (agents / "com.ciao.server.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "com.ciao.server",
                "EnvironmentVariables": {
                    "CIAO_WORKSPACE": str(tmp_path / "old-install")
                },
            }
        )
    )

    assert workspace_move.registered_workspace() == source.resolve()


def test_read_operation_defaults_app_false_for_old_records(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    root.mkdir(parents=True)
    # A 1.x launchd-job record carries neither field.
    (root / "operation.json").write_text(
        json.dumps(
            {
                "id": "op-old",
                "phase": "stopping",
                "source": str(tmp_path / "a"),
                "target": str(tmp_path / "b"),
                "started_at": "2025-01-01T00:00:00+00:00",
                "updated_at": "2025-01-01T00:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    op = workspace_move.read_operation(root)
    assert op is not None
    assert op.app is False
    assert op.runtime_root == ""


def test_operation_round_trips_app_and_runtime_root(tmp_path: Path) -> None:
    root = tmp_path / "state"
    _store(root, _op(tmp_path / "a", tmp_path / "b", runtime_root="/a/.runtime"))
    assert _state_dir(root) == _op(
        tmp_path / "a", tmp_path / "b", runtime_root="/a/.runtime"
    )


# ── the forward move across the exec restart ────────────────────────


def _forward_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A workspace at ``source``, a pointer naming it, and a fresh stop record."""
    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    target = tmp_path / "moved"
    app_engine.write_pointer(source, source / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )
    return source, target, root


def test_forward_moves_rewrites_and_repoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)

    assert workspace_move_app.before_boot(state_dir=root) == "moved"

    assert not source.exists()
    assert (target / ".env").is_file()
    # The workspace's own paths followed the folder.
    env = (target / ".env").read_text(encoding="utf-8")
    assert f"CIAO_RUNTIME_ROOT={target / '.runtime'}" in env
    state = json.loads((target / ".runtime" / "state.json").read_text())
    assert state["vault"] == str(target / "vault")
    # The pointer was repointed after the rename and the rewrites.
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == target.resolve()
    assert pointer.runtime_root == (target / ".runtime").resolve()
    # The record waits at verifying for the booted engine's own check.
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "verifying"
    # And no launchd job was planted: the exec restart needs none.
    agents = Path.home() / "Library" / "LaunchAgents"
    assert not agents.exists() or not any(agents.iterdir())


def test_pointer_untouched_when_rewrite_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)

    real_rewrite = workspace_move.rewrite_workspace_state
    real_write = app_engine.write_pointer
    writes: list[Path] = []

    def rewrite(root_path: Path, old: str, new: str) -> None:
        if old == str(source):  # the forward rewrite
            raise RuntimeError("rewrite failed")
        real_rewrite(root_path, old, new)

    def write_pointer(w: Path, runtime_root: Path, **kwargs: object) -> Path:
        writes.append(w)
        return real_write(w, runtime_root, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(workspace_move, "rewrite_workspace_state", rewrite)
    monkeypatch.setattr(app_engine, "write_pointer", write_pointer)

    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "rolled_back"
    # The folder is back, and the pointer named the source — it was never
    # repointed onto the target, because the rewrite failed first.
    assert source.is_dir()
    assert not target.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == source.resolve()
    assert pointer.runtime_root == (source / ".runtime").resolve()
    assert not any(p.resolve() == target.resolve() for p in writes)
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "rolled_back"
    assert "rewrite failed" in final.error


# ── the rollback of an interrupted or failed move ───────────────────


def _mid_move_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> tuple[Path, Path, Path]:
    """The boot died in ``phase``: the folder sits at the target, the source is gone."""
    _app_mode(monkeypatch)
    # The forward rename already moved the workspace to the target (and, past
    # the rewriting phase, its recorded paths name the target). Nothing exists
    # at the source path yet.
    source = tmp_path / "ws"
    target = _workspace(tmp_path / "moved")
    app_engine.write_pointer(target, target / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(
            source,
            target,
            phase=phase,
            runtime_root=str(source / ".runtime"),
            updated_ago=workspace_move_app.VERIFY_GRACE_S + 60,
        ),
    )
    return source, target, root


def test_verifying_on_boot_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    assert source.is_dir()
    assert not target.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == source.resolve()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "rolled_back"
    assert final.error == "Ciaobot stopped while starting from the new folder"
    state = json.loads((source / ".runtime" / "state.json").read_text())
    assert state["vault"] == str(source / "vault")


@pytest.mark.parametrize("phase", ["moving", "rewriting", "repointing", "rolling_back"])
def test_mid_move_phases_roll_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, phase)

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    assert source.is_dir()
    assert not target.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == source.resolve()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "rolled_back"
    assert final.error == "Ciaobot stopped in the middle of the move"


def test_rollback_pointer_follows_disk_when_rename_back_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")

    real_rename = os.rename
    rename_calls: list[tuple[Path, Path]] = []

    def rename(src: Path, dst: Path, **kwargs: object) -> None:
        rename_calls.append((Path(src), Path(dst)))
        if Path(src) == target and Path(dst) == source:
            raise OSError("rename back failed")
        return real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)
    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "rollback_failed"
    assert target.is_dir()
    # The pointer follows the disk: still the target, with the runtime root
    # rewritten to match, so this boot is a real workspace, not the wizard.
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == target.resolve()
    assert pointer.runtime_root == (target / ".runtime").resolve()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "rollback_failed"
    assert "rollback: moving the folder back" in final.error


def _denial_message(folder: Path) -> str:
    return (
        f"macOS denied Ciaobot access to {folder}: allow it in System Settings "
        "→ Privacy & Security → Files and Folders (Ciaobot), then move the "
        "workspace again."
    )


def test_destination_privacy_denial_fails_without_renaming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)

    def probe(target_path: Path) -> None:
        raise PermissionError(errno.EPERM, "Operation not permitted")

    monkeypatch.setattr(workspace_move, "_probe_destination_access", probe)
    monkeypatch.setattr(sys, "platform", "darwin")

    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "failed"
    assert source.is_dir(), "the probe denial must refuse before anything moves"
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == source.resolve()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "failed"
    assert final.denied is True
    assert final.error == _denial_message(target)
    from ciao import workspace_access

    assert workspace_access.read_denial() != ""


def test_before_boot_never_raises(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _app_mode(monkeypatch)
    # A record some reader cannot parse is nothing to act on, never a crash.
    root = tmp_path / "state"
    root.mkdir(parents=True)
    (root / "operation.json").write_text("{not json at all", encoding="utf-8")
    assert workspace_move_app.before_boot(state_dir=root) == "none"

    # A rewrite that raises must not kill the boot either: the move is undone
    # and the boot continues on the old folder.
    source = _workspace(tmp_path / "ws")
    target = tmp_path / "moved"
    app_engine.write_pointer(source, source / ".runtime")
    op_root = _store(
        tmp_path / "state2",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )
    real_prefix = workspace_move.rewrite_prefix
    prefix_calls: list[str] = []

    def prefix(value: str, old: str, new: str) -> str:
        # The first call is the pre-move liveness probe; the repoint (after
        # the rename) is the one that blows up.
        prefix_calls.append(value)
        if len(prefix_calls) > 1:
            raise RuntimeError("prefix blew up")
        return real_prefix(value, old, new)

    monkeypatch.setattr(workspace_move, "rewrite_prefix", prefix)
    result = workspace_move_app.before_boot(state_dir=op_root)
    # It must not raise: the boot reports a rollback result and continues on
    # the folder the pointer names.
    assert result in ("rolled_back", "rollback_failed")
    assert source.is_dir()
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == source.resolve()

    # And even a dying record write (a full disk, say) is one stderr line.
    failing_root = _store(
        tmp_path / "state3",
        _op(tmp_path / "ws2", tmp_path / "ws3", app=True),
    )
    monkeypatch.setattr(
        workspace_move,
        "write_operation",
        lambda op, state_dir: (_ for _ in ()).throw(OSError("disk full")),
    )
    result = workspace_move_app.before_boot(state_dir=failing_root)
    # Record writes are best-effort: the refusal is the answer, and the
    # failed write is one stderr line.
    assert result == "failed"
    err = capsys.readouterr().err
    assert any(
        line.startswith("workspace move:") for line in err.splitlines()
    )


def test_not_app_mode_is_a_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _app_mode(monkeypatch, bundled=False)
    source = _workspace(tmp_path / "ws")
    target = tmp_path / "moved"
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "none"

    assert source.is_dir()
    assert not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "stopping"


def test_non_app_record_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A 1.x launchd-job record is never acted on by the app-mode engine."""
    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    target = tmp_path / "moved"
    root = _store(
        tmp_path / "state",
        _op(source, target, app=False, runtime_root=str(source / ".runtime")),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "none"

    assert source.is_dir()
    assert not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "stopping"


def test_stale_stopping_is_failed_without_moving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    app_engine.write_pointer(source, source / ".runtime")
    target = tmp_path / "moved"
    old = datetime.now(UTC) - timedelta(seconds=int(workspace_move_app.STALE_STOPPING_S) + 120)
    op = workspace_move.MoveOperation(
        id="op-1",
        phase="stopping",
        source=str(source),
        target=str(target),
        started_at=old.isoformat(timespec="seconds"),
        updated_at=old.isoformat(timespec="seconds"),
        app=True,
        runtime_root=str(source / ".runtime"),
    )
    root = _store(tmp_path / "state", op)
    now = time.time()

    assert (
        workspace_move_app.before_boot(state_dir=root, now=lambda: now) == "failed"
    )

    assert source.is_dir()
    assert not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "failed"
    assert final.error == workspace_move_app._INTERRUPTED_BEFORE_MOVE


def test_server_lock_held_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support.locks import lock_exclusive

    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    app_engine.write_pointer(source, source / ".runtime")
    target = tmp_path / "moved"
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )

    # Another engine (say, an old service-mode one) still serves the source.
    with (source / ".runtime" / "server.lock").open("rb") as held:
        lock_exclusive(held.fileno(), blocking=False)
        result = workspace_move_app.before_boot(state_dir=root)

    assert result == "skipped"
    assert source.is_dir()
    assert not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "stopping"


def test_signals_ignored_during_forward_and_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)

    original_term = signal.getsignal(signal.SIGTERM)
    original_hup = getattr(signal, "SIGHUP", None)
    original_hup_handler = (
        signal.getsignal(original_hup) if original_hup is not None else None
    )
    seen: list[object] = []
    real_rename = os.rename

    def rename(src: Path, dst: Path, **kwargs: object) -> None:
        seen.append(signal.getsignal(signal.SIGTERM))
        return real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)

    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "moved"
    assert seen, "the forward move renames the folder"
    assert seen[0] == signal.SIG_IGN
    assert signal.getsignal(signal.SIGTERM) == original_term
    if original_hup is not None:
        assert original_hup_handler is not None
        assert signal.getsignal(original_hup) == original_hup_handler


# ── a failed record write never strands the data ────────────────────


def _fail_record_writes(
    monkeypatch: pytest.MonkeyPatch, *, from_call: int, persistent: bool
) -> Callable[[], None]:
    """Fail the Nth record write (and every later one when ``persistent``).

    Every other write delegates to the real writer. Returns a function that
    restores the real writer, for the "following boot" half of a test.
    """
    real = workspace_move.write_operation
    calls = [0]

    def write(op: workspace_move.MoveOperation, state_dir: Path | None = None) -> None:
        calls[0] += 1
        if calls[0] == from_call or (persistent and calls[0] > from_call):
            raise OSError(errno.ENOSPC, "No space left on device")
        real(op, state_dir)

    monkeypatch.setattr(workspace_move, "write_operation", write)
    return lambda: monkeypatch.setattr(workspace_move, "write_operation", real)


def _assert_data_where_pointer_names(source: Path, target: Path) -> Path:
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert (pointer.workspace / ".env").is_file(), "the pointer names a folder with the data"
    assert (pointer.workspace / ".runtime" / "state.json").is_file()
    assert pointer.workspace in (source.resolve(), target.resolve())
    return pointer.workspace


@pytest.mark.parametrize("persistent", [False, True])
@pytest.mark.parametrize("from_call", [2, 3, 4])
def test_forward_record_write_failure_after_rename_keeps_the_data_reachable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    from_call: int,
    persistent: bool,
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)
    restore = _fail_record_writes(monkeypatch, from_call=from_call, persistent=persistent)

    result = workspace_move_app.before_boot(state_dir=root)

    # The record is advisory: the folder is back and the pointer names it.
    assert result == "rolled_back"
    assert source.is_dir() and not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()
    state = json.loads((source / ".runtime" / "state.json").read_text())
    assert state["vault"] == str(source / "vault")

    # A following boot (the record may be stale) must not bury or move it.
    restore()
    again = workspace_move_app.before_boot(state_dir=root)
    assert again in ("none", "rolled_back")
    assert source.is_dir() and not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()


def test_forward_record_write_failure_before_rename_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)
    _fail_record_writes(monkeypatch, from_call=1, persistent=True)

    assert workspace_move_app.before_boot(state_dir=root) == "failed"

    assert source.is_dir() and not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()


@pytest.mark.parametrize("persistent", [False, True])
def test_rollback_record_write_failure_still_renames_back_and_repoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, persistent: bool
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    restore = _fail_record_writes(monkeypatch, from_call=1, persistent=persistent)

    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "rolled_back"
    assert source.is_dir() and not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()

    restore()
    workspace_move_app.before_boot(state_dir=root)
    assert source.is_dir() and not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()


def test_rollback_record_write_failure_with_rename_back_failure_names_the_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _fail_record_writes(monkeypatch, from_call=1, persistent=True)
    real_rename = os.rename

    def rename(src: Path, dst: Path, **kwargs: object) -> None:
        if Path(src) == target:
            raise OSError("rename back failed")
        return real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)

    assert workspace_move_app.before_boot(state_dir=root) == "rollback_failed"

    assert target.is_dir()
    assert _assert_data_where_pointer_names(source, target) == target.resolve()


# ── the record is validated before anything moves ───────────────────


def _refusal_setup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """A valid forward setup; tests then swap one thing for something invalid."""
    return _forward_setup(tmp_path, monkeypatch)


def _expect_refused(root: Path, *folders: Path, reason: str) -> None:
    result = workspace_move_app.before_boot(state_dir=root)
    assert result == "failed"
    final = _state_dir(root)
    assert final is not None and final.phase == "failed"
    assert reason in final.error
    for folder in folders:
        assert (folder / ".env").is_file(), f"{folder} must be untouched"
    pointer = app_engine.read_pointer()
    assert pointer is not None


def test_refuses_a_symlinked_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _target, _root = _refusal_setup(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "link"
    link.symlink_to(elsewhere, target_is_directory=True)
    root = _store(
        tmp_path / "state",
        _op(source, link, runtime_root=str(source / ".runtime")),
    )

    _expect_refused(root, source, reason="symbolic link")
    assert list(elsewhere.iterdir()) == []


def test_refuses_a_symlinked_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    real = _workspace(tmp_path / "real")
    link = tmp_path / "ws"
    link.symlink_to(real, target_is_directory=True)
    app_engine.write_pointer(real, real / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(link, tmp_path / "moved", runtime_root=str(real / ".runtime")),
    )

    _expect_refused(root, real, reason="symbolic link")
    assert not (tmp_path / "moved").exists()


@pytest.mark.parametrize(
    ("inside", "reason"),
    [
        ("target-in-source", "inside one another"),
        ("source-in-target", "inside one another"),
        ("same", "same folder"),
    ],
)
def test_refuses_overlapping_or_equal_folders(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, inside: str, reason: str
) -> None:
    source, _target, _root = _refusal_setup(tmp_path, monkeypatch)
    target = {
        "target-in-source": source / "inner",
        "source-in-target": tmp_path,
        "same": source,
    }[inside]
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )

    _expect_refused(root, source, reason=reason)
    assert not (source / "inner").exists()


def test_refuses_a_non_empty_target_and_keeps_its_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    target.mkdir()
    (target / "keep.txt").write_text("mine", encoding="utf-8")

    _expect_refused(root, source, reason="not empty")
    assert (target / "keep.txt").read_text(encoding="utf-8") == "mine"


def test_refuses_a_target_that_is_a_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    target.write_text("a file", encoding="utf-8")

    _expect_refused(root, source, reason="not empty")
    assert target.read_text(encoding="utf-8") == "a file"


def test_an_empty_target_directory_is_replaced_by_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    target.mkdir()

    assert workspace_move_app.before_boot(state_dir=root) == "moved"

    assert not source.exists()
    assert (target / ".env").is_file()


def test_refuses_a_source_the_pointer_does_not_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _root = _refusal_setup(tmp_path, monkeypatch)
    other = _workspace(tmp_path / "other")
    app_engine.write_pointer(other, other / ".runtime")
    root = tmp_path / "state"

    _expect_refused(root, source, other, reason="not the workspace Ciaobot is using")
    assert not target.exists()


def test_refuses_when_no_pointer_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    (app_engine.state_dir() / app_engine.WORKSPACE_POINTER_NAME).unlink()

    result = workspace_move_app.before_boot(state_dir=root)

    assert result == "failed"
    assert source.is_dir() and not target.exists()


@pytest.mark.parametrize("field", ["source", "target", "runtime_root"])
@pytest.mark.parametrize("value", ["relative/path", ""])
def test_refuses_relative_or_empty_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: str
) -> None:
    source, target, _root = _refusal_setup(tmp_path, monkeypatch)
    op = _op(source, target, runtime_root=str(source / ".runtime"))
    setattr(op, field, value)
    root = _store(tmp_path / "state", op)

    _expect_refused(root, source, reason="not an absolute path")
    assert not target.exists()


def test_refuses_a_missing_target_parent_without_creating_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _target, _root = _refusal_setup(tmp_path, monkeypatch)
    target = tmp_path / "nowhere" / "moved"
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )

    _expect_refused(root, source, reason="does not exist")
    assert not (tmp_path / "nowhere").exists()


def test_refuses_a_source_that_is_not_a_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    source = tmp_path / "ws"
    source.write_text("not a folder", encoding="utf-8")
    keep = _workspace(tmp_path / "keep")
    app_engine.write_pointer(keep, keep / ".runtime")
    pointer_file = app_engine.state_dir() / app_engine.WORKSPACE_POINTER_NAME
    payload = json.loads(pointer_file.read_text())
    payload["workspace"] = str(source)
    pointer_file.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(pointer_file, 0o600)
    root = _store(
        tmp_path / "state",
        _op(source, tmp_path / "moved", runtime_root=str(keep / ".runtime")),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "failed"
    assert source.read_text(encoding="utf-8") == "not a folder"
    assert not (tmp_path / "moved").exists()


def test_a_world_readable_record_is_not_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    os.chmod(root / "operation.json", 0o644)

    assert workspace_move_app.before_boot(state_dir=root) == "none"
    assert source.is_dir() and not target.exists()


def test_a_symlinked_record_is_not_acted_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _refusal_setup(tmp_path, monkeypatch)
    real = root / "real.json"
    (root / "operation.json").rename(real)
    (root / "operation.json").symlink_to(real)

    assert workspace_move_app.before_boot(state_dir=root) == "none"
    assert source.is_dir() and not target.exists()


def test_a_future_dated_stopping_record_does_not_bypass_the_age_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _root = _refusal_setup(tmp_path, monkeypatch)
    op = _op(source, target, runtime_root=str(source / ".runtime"))
    op.updated_at = (datetime.now(UTC) + timedelta(days=365)).isoformat(timespec="seconds")
    root = _store(tmp_path / "state", op)

    assert workspace_move_app.before_boot(state_dir=root) == "failed"

    assert source.is_dir() and not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.error == workspace_move_app._INTERRUPTED_BEFORE_MOVE


def test_recover_refusal_is_rollback_failed_and_touches_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, _root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    other = _workspace(tmp_path / "other")
    app_engine.write_pointer(other, other / ".runtime")
    root = tmp_path / "state"

    assert workspace_move_app.before_boot(state_dir=root) == "rollback_failed"

    assert target.is_dir() and not source.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == other.resolve()


# ── spelling: the runtime root follows a differently spelled source ──


@pytest.mark.skipif(sys.platform == "win32", reason="needs symlinks")
def test_runtime_root_is_rewritten_when_the_record_spells_paths_through_an_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    source = _workspace(real / "ws")
    app_engine.write_pointer(source, source / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(
            alias / "ws",
            alias / "moved",
            runtime_root=str(alias / "ws" / ".runtime"),
        ),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "moved"

    target = real / "moved"
    pointer = app_engine.read_pointer()
    assert pointer is not None
    assert pointer.workspace == target.resolve()
    assert pointer.runtime_root == (target / ".runtime").resolve()
    assert (target / ".runtime").is_dir()


# ── rollback with both folders present ──────────────────────────────


def test_rollback_moves_the_data_over_an_empty_fresh_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    source.mkdir()  # a fresh, empty folder at the old path

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    assert not target.exists()
    assert _assert_data_where_pointer_names(source, target) == source.resolve()


def test_rollback_with_a_populated_source_and_empty_target_points_at_the_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    # Data is at the source; the target is a fresh empty folder. The pointer
    # names the empty target, which must not win.
    target.rename(source)
    target.mkdir()

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    assert _assert_data_where_pointer_names(source, target) == source.resolve()


def test_rollback_with_both_folders_populated_fails_and_moves_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _workspace(source)
    before = app_engine.read_pointer()
    assert before is not None

    assert workspace_move_app.before_boot(state_dir=root) == "rollback_failed"

    assert (source / ".env").is_file() and (target / ".env").is_file()
    after = app_engine.read_pointer()
    assert after == before
    final = _state_dir(root)
    assert final is not None and "hold files" in final.error


def _case_insensitive(tmp_path: Path) -> bool:
    probe = tmp_path / "CaseProbe"
    probe.mkdir()
    return (tmp_path / "caseprobe").exists()


def test_case_only_rename_forward_and_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive volume")
    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "Ws")
    target = tmp_path / "ws"
    app_engine.write_pointer(source, source / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(source, target, runtime_root=str(source / ".runtime")),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "moved"
    assert "ws" in os.listdir(tmp_path) and "Ws" not in os.listdir(tmp_path)

    # The verify never came: the next boot (after the grace) puts the
    # spelling back instead of calling the one folder "both present".
    final = _state_dir(root)
    assert final is not None
    final.updated_at = (
        datetime.now(UTC) - timedelta(seconds=workspace_move_app.VERIFY_GRACE_S + 60)
    ).isoformat(timespec="seconds")
    _store(root, final)

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"
    assert "Ws" in os.listdir(tmp_path) and "ws" not in os.listdir(tmp_path)
    assert (tmp_path / "Ws" / ".env").is_file()


# ── S3b-1 is inert until S3b-2 writes a record ──────────────────────


def test_no_record_means_before_boot_does_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    app_engine.write_pointer(source, source / ".runtime")
    pointer_file = app_engine.state_dir() / app_engine.WORKSPACE_POINTER_NAME
    before = pointer_file.read_bytes()
    root = tmp_path / "state"

    assert workspace_move_app.before_boot(state_dir=root) == "none"

    assert source.is_dir()
    assert pointer_file.read_bytes() == before
    assert not (root / workspace_move.OPERATION_NAME).exists()
    assert {p.name for p in tmp_path.iterdir()} - {"home"} == {"state", "ws"}


# Modules allowed to write an app-mode operation record. S3b-2's move route
# is added here when it lands; until then nothing in ciao/ may.
_APP_RECORD_WRITERS: frozenset[str] = frozenset()


def test_nothing_in_ciao_writes_an_app_record() -> None:
    import re

    root = Path(workspace_move_app.__file__).resolve().parent
    patterns = [
        re.compile(r"MoveOperation\((?:[^()]|\([^()]*\))*?\bapp\s*=\s*True", re.S),
        re.compile(r"\.app\s*=\s*True"),
        re.compile(r"""["']app["']\s*:\s*True"""),
    ]
    offenders = []
    for path in root.rglob("*.py"):
        if "static" in path.relative_to(root).parts:
            continue
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8")
        if any(pattern.search(text) for pattern in patterns) and rel not in _APP_RECORD_WRITERS:
            offenders.append(rel)
    assert offenders == [], "S3b-1 must not create app-mode records; that is S3b-2's route"


def test_a_written_app_record_is_always_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    op = _op(tmp_path / "a", tmp_path / "b", runtime_root=str(tmp_path / "a" / ".runtime"))
    for mask in (0o000, 0o022, 0o077):
        root = tmp_path / f"state-{mask:o}"
        old = os.umask(mask)
        try:
            workspace_move.write_operation(op, root)
        finally:
            os.umask(old)
        record = root / workspace_move.OPERATION_NAME
        assert record.stat().st_mode & 0o777 == 0o600
        assert workspace_move.read_operation_strict(root) is not None

    # Replacing a record some other process left world-readable.
    root = tmp_path / "state-replace"
    root.mkdir()
    record = root / workspace_move.OPERATION_NAME
    record.write_text("{}", encoding="utf-8")
    record.chmod(0o644)
    workspace_move.write_operation(op, root)
    assert workspace_move.read_operation_strict(root) is not None


@pytest.mark.skipif(sys.platform != "darwin", reason="needs macOS ACLs")
def test_a_written_app_record_ignores_an_inherited_acl(tmp_path: Path) -> None:
    import subprocess

    root = tmp_path / "state"
    root.mkdir()
    made = subprocess.run(
        ["chmod", "+a", "everyone allow read,file_inherit", str(root)],
        capture_output=True,
    )
    if made.returncode != 0:
        pytest.skip("cannot set an ACL here")
    op = _op(tmp_path / "a", tmp_path / "b")
    # The writer verifies the file it created (as the pointer writer does): an
    # inherited ACL is refused outright, never written as a record this
    # process could not read back.
    try:
        workspace_move.write_operation(op, root)
    except OSError:
        assert not (root / workspace_move.OPERATION_NAME).exists()
        assert not any(p.name.startswith(".operation.json") for p in root.iterdir())
    else:
        assert workspace_move.read_operation_strict(root) is not None


# ── an interrupted rollback resumes ─────────────────────────────────


def test_rollback_resumes_after_dying_post_rename_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "rolling_back")
    real_rewrite = workspace_move.rewrite_workspace_state

    def dies(root_path: Path, old: str, new: str) -> None:
        raise RuntimeError("process died here")

    monkeypatch.setattr(workspace_move, "rewrite_workspace_state", dies)
    assert workspace_move_app.before_boot(state_dir=root) == "rollback_failed"
    # The folder is back but its paths still name the target.
    assert source.is_dir() and not target.exists()
    assert str(target) in (source / ".env").read_text(encoding="utf-8")
    final = _state_dir(root)
    assert final is not None and "rewriting the folder's paths back" in final.error
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == source.resolve()

    # The boot that finds the record still `rolling_back` (it died before the
    # failure was recorded) finishes the job: target absent, source present.
    monkeypatch.setattr(workspace_move, "rewrite_workspace_state", real_rewrite)
    final.phase = "rolling_back"
    final.updated_at = (
        datetime.now(UTC) - timedelta(seconds=workspace_move_app.VERIFY_GRACE_S + 60)
    ).isoformat(timespec="seconds")
    _store(root, final)

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    assert str(target) not in (source / ".env").read_text(encoding="utf-8")
    state = json.loads((source / ".runtime" / "state.json").read_text())
    assert state["vault"] == str(source / "vault")


def test_rollback_restores_both_path_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _app_mode(monkeypatch)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)
    source = _workspace(real / "ws")
    # One file spells the folder through the alias, one as resolved.
    (source / ".env").write_text(
        f"PWA_PORT=8443\nCIAO_RUNTIME_ROOT={alias / 'ws' / '.runtime'}\n", encoding="utf-8"
    )
    target = tmp_path / "moved"
    app_engine.write_pointer(source, source / ".runtime")
    root = _store(
        tmp_path / "state",
        _op(alias / "ws", target, runtime_root=str(source.resolve() / ".runtime")),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "moved"
    assert "alias" not in (target / ".env").read_text(encoding="utf-8")

    final = _state_dir(root)
    assert final is not None
    final.updated_at = (
        datetime.now(UTC) - timedelta(seconds=workspace_move_app.VERIFY_GRACE_S + 60)
    ).isoformat(timespec="seconds")
    _store(root, final)
    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    for name in (".env", ".runtime/state.json"):
        text = (real / "ws" / name).read_text(encoding="utf-8")
        assert str(target) not in text, name
    assert f"CIAO_RUNTIME_ROOT={real.resolve() / 'ws' / '.runtime'}" in (
        real / "ws" / ".env"
    ).read_text(encoding="utf-8")


# ── nothing raises out of the failure handler before the rollback ───────


@pytest.mark.parametrize("raiser", ["_record_denied_folder", "_denied_folder"])
def test_denial_bookkeeping_failure_cannot_skip_the_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, raiser: str
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_move, "_is_privacy_denial", lambda exc, platform=None: True)

    def rewrite(root_path: Path, old: str, new: str) -> None:
        raise PermissionError(errno.EPERM, "denied")

    def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError(f"{raiser} blew up")

    monkeypatch.setattr(workspace_move, "rewrite_workspace_state", rewrite)
    monkeypatch.setattr(workspace_move, raiser, boom)
    # The probe (before the rename) would also be "denied": let it through.
    monkeypatch.setattr(workspace_move, "_probe_destination_access", lambda target: None)

    result = workspace_move_app.before_boot(state_dir=root)

    # The rename happened, so the rollback must have put the folder back.
    assert result in ("rolled_back", "rollback_failed")
    assert source.is_dir() and not target.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == source.resolve()


# ── clock skew on a verifying record ────────────────────────────────


def _old() -> str:
    return (
        datetime.now(UTC) - timedelta(seconds=workspace_move_app.VERIFY_GRACE_S + 60)
    ).isoformat(timespec="seconds")


@pytest.mark.parametrize(
    "stamp",
    [
        (datetime.now(UTC) + timedelta(days=30)).isoformat(timespec="seconds"),
        "",
        "not a timestamp",
    ],
    ids=["future", "empty", "unparsable"],
)
def test_untrustworthy_verifying_stamp_reads_as_young(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    op = _state_dir(root)
    assert op is not None
    op.updated_at = stamp
    _store(root, op)
    # The file is ancient, so only the stamp rule can protect it.
    old = time.time() - workspace_move_app.VERIFY_GRACE_S - 600
    os.utime(root / workspace_move.OPERATION_NAME, (old, old))

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    assert target.is_dir() and not source.exists()
    final = _state_dir(root)
    assert final is not None and final.phase == "verifying"


def test_verifying_record_with_a_fresh_mtime_is_young_despite_an_old_stamp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    now = time.time()
    os.utime(root / workspace_move.OPERATION_NAME, (now, now))  # written just now

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"
    assert target.is_dir() and not source.exists()


def test_a_backward_clock_step_reads_a_verifying_record_as_young(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    # The record is old by the real clock; the boot's clock then stepped back
    # a day, so the stamp and the file are both "in the future".
    behind = lambda: time.time() - 86400  # noqa: E731

    assert workspace_move_app.before_boot(state_dir=root, now=behind) == "skipped"
    assert target.is_dir() and not source.exists()


@pytest.mark.parametrize("stamp", ["", "garbage"])
def test_stopping_with_an_untrustworthy_stamp_still_reads_as_old(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stamp: str
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)
    op = _state_dir(root)
    assert op is not None
    op.updated_at = stamp
    _store(root, op)

    assert workspace_move_app.before_boot(state_dir=root) == "failed"
    assert source.is_dir() and not target.exists()


# ── the pointer always names a folder that exists ───────────────────


def _pointer_at_vanished_source(source: Path) -> None:
    app_engine.write_pointer(source, source / ".runtime")
    assert not source.exists()


def test_live_engine_skip_still_repairs_a_vanished_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support.locks import lock_exclusive

    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _pointer_at_vanished_source(source)

    with (target / ".runtime" / "server.lock").open("rb") as held:
        lock_exclusive(held.fileno(), blocking=False)
        assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    assert target.is_dir() and not source.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == target.resolve()


def test_young_verifying_skip_still_repairs_a_vanished_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    now = time.time()
    os.utime(root / workspace_move.OPERATION_NAME, (now, now))
    _pointer_at_vanished_source(source)

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == target.resolve()
    assert target.is_dir() and not source.exists()


def test_unreadable_record_still_repairs_a_vanished_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _pointer_at_vanished_source(source)
    (root / workspace_move.OPERATION_NAME).chmod(0o644)  # fails the strict read

    assert workspace_move_app.before_boot(state_dir=root) == "none"

    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == target.resolve()
    assert target.is_dir() and not source.exists()  # repaired, not rolled back


def test_second_concurrent_boot_leaves_a_held_move_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _forward_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_move_app, "_LOCK_WAIT_S", 0.2)
    handle = workspace_move._lock(root)
    try:
        assert workspace_move_app.before_boot(state_dir=root) == "skipped"
    finally:
        handle.close()

    assert source.is_dir() and not target.exists()
    final = _state_dir(root)
    assert final is not None and final.phase == "stopping"


def test_second_boot_waits_for_the_first_then_sees_its_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading

    source, target, root = _forward_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(workspace_move_app, "_LOCK_WAIT_S", 10.0)
    handle = workspace_move._lock(root)
    released = threading.Timer(0.3, handle.close)
    released.start()
    try:
        assert workspace_move_app.before_boot(state_dir=root) == "moved"
    finally:
        released.join()
        handle.close()
    assert target.is_dir() and not source.exists()
    # A third boot, now inside the verify window, leaves it alone.
    assert workspace_move_app.before_boot(state_dir=root) == "skipped"
    assert target.is_dir() and not source.exists()


def _break_rollback(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*args: object, **kwargs: object) -> str:
        raise RuntimeError("rollback blew up")

    monkeypatch.setattr(workspace_move_app, "_rollback", boom)


def test_emergency_repair_repairs_the_pointer_under_the_lock_with_signals_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _pointer_at_vanished_source(source)
    _break_rollback(monkeypatch)
    seen: dict[str, object] = {}
    real_repair = workspace_move_app._repair_pointer

    def spy(paths: workspace_move_app._Paths, errors: list[str]) -> None:
        seen["sigterm"] = signal.getsignal(signal.SIGTERM)
        seen["locked"] = workspace_move.job_running(root)
        real_repair(paths, errors)

    monkeypatch.setattr(workspace_move_app, "_repair_pointer", spy)

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    assert seen["sigterm"] == signal.SIG_IGN
    assert seen["locked"] is True
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == target.resolve()
    assert not workspace_move.job_running(root)  # and the lock is released


def test_emergency_repair_does_nothing_under_a_live_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _pointer_at_vanished_source(source)
    _break_rollback(monkeypatch)
    calls = [0]

    def alive(paths: workspace_move_app._Paths) -> bool:
        calls[0] += 1
        return calls[0] > 1  # the main path saw nobody; the emergency path does

    monkeypatch.setattr(workspace_move_app, "_engine_alive", alive)

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    assert calls[0] >= 2
    assert app_engine.read_pointer() is None  # untouched: still the vanished source


def test_emergency_repair_does_nothing_while_another_boot_holds_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _pointer_at_vanished_source(source)
    handle = workspace_move._lock(root)
    try:
        workspace_move_app._emergency_repair(root)
    finally:
        handle.close()
    assert app_engine.read_pointer() is None


# ── pointer schema and denial scoping ───────────────────────────────


@pytest.mark.parametrize("schema", [1.0, True, 2, "1"])
def test_pointer_workspace_rejects_what_read_pointer_rejects(
    tmp_path: Path, schema: object
) -> None:
    source = _workspace(tmp_path / "ws")
    app_engine.write_private_json(
        app_engine.state_dir() / app_engine.WORKSPACE_POINTER_NAME,
        {
            "schema": schema,
            "workspace": str(source),
            "runtime_root": str(source / ".runtime"),
        },
    )
    assert app_engine.read_pointer() is None
    assert app_engine.read_pointer_record() is None
    assert workspace_move_app._pointer_workspace() is None


def test_pointer_record_keeps_a_vanished_workspace(tmp_path: Path) -> None:
    gone = tmp_path / "gone"
    app_engine.write_pointer(gone, gone / ".runtime")
    assert app_engine.read_pointer() is None
    record = app_engine.read_pointer_record()
    assert record is not None and record.workspace == gone.resolve()
    assert workspace_move_app._pointer_workspace() == gone.resolve()


def _write_denial(message: str, *, age: float) -> None:
    from ciao import workspace_access

    workspace_access.record_denial(message)
    stamp = time.time() - age
    os.utime(workspace_access.denial_path(), (stamp, stamp))


def test_a_stale_denial_does_not_replace_the_rollback_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    # Recorded long before this operation began: somebody else's.
    _write_denial("macOS denied some other folder", age=7 * 86400)

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    final = _state_dir(root)
    assert final is not None
    assert final.error == "Ciaobot stopped while starting from the new folder"
    assert final.denied is False


def test_a_denial_recorded_during_the_operation_is_the_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    _write_denial("macOS denied Ciaobot access to /Users/x/Documents", age=0)

    assert workspace_move_app.before_boot(state_dir=root) == "rolled_back"

    final = _state_dir(root)
    assert final is not None
    assert final.error == "macOS denied Ciaobot access to /Users/x/Documents"
    assert final.denied is True


# ── never rename while an engine may be alive ───────────────────────


def test_fresh_verifying_record_is_not_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second boot (or an ordinary restart) inside the verifying window."""
    source, target, _root = _mid_move_setup(tmp_path, monkeypatch, "verifying")
    root = _store(
        tmp_path / "state",
        _op(
            source,
            target,
            phase="verifying",
            runtime_root=str(source / ".runtime"),
            updated_ago=5,
        ),
    )

    assert workspace_move_app.before_boot(state_dir=root) == "skipped"

    assert target.is_dir() and not source.exists()
    pointer = app_engine.read_pointer()
    assert pointer is not None and pointer.workspace == target.resolve()
    final = _state_dir(root)
    assert final is not None and final.phase == "verifying"


def test_aged_verifying_record_with_a_live_engine_is_not_rolled_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support.locks import lock_exclusive

    source, target, root = _mid_move_setup(tmp_path, monkeypatch, "verifying")

    with (target / ".runtime" / "server.lock").open("rb") as held:
        lock_exclusive(held.fileno(), blocking=False)
        result = workspace_move_app.before_boot(state_dir=root)

    assert result == "skipped"
    assert target.is_dir() and not source.exists()


def test_live_engine_on_an_external_runtime_root_blocks_the_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from ciao.os_support.locks import lock_exclusive

    _app_mode(monkeypatch)
    source = _workspace(tmp_path / "ws")
    runtime = tmp_path / "rt"
    runtime.mkdir()
    (runtime / "server.lock").touch()
    # The workspace's .env names no external runtime; only the record does.
    app_engine.write_pointer(source, runtime)
    root = _store(
        tmp_path / "state",
        _op(source, tmp_path / "moved", runtime_root=str(runtime)),
    )

    with (runtime / "server.lock").open("rb") as held:
        lock_exclusive(held.fileno(), blocking=False)
        result = workspace_move_app.before_boot(state_dir=root)

    assert result == "skipped"
    assert source.is_dir() and not (tmp_path / "moved").exists()


# ── signals, threads and devices ────────────────────────────────────


def test_signals_ignored_tolerates_a_missing_previous_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = signal.getsignal
    installed: list[tuple[int, object]] = []
    real_signal = signal.signal

    monkeypatch.setattr(
        signal, "getsignal", lambda sig: None if sig == signal.SIGTERM else real(sig)
    )

    def recording(sig: int, handler: object) -> object:
        installed.append((sig, handler))
        return real_signal(sig, handler)  # type: ignore[arg-type]

    monkeypatch.setattr(signal, "signal", recording)
    original = real(signal.SIGTERM)
    try:
        with workspace_move_app._signals_ignored():
            pass
    finally:
        real_signal(signal.SIGTERM, original)

    assert (signal.SIGTERM, signal.SIG_DFL) in installed


def test_signals_ignored_from_a_worker_thread_does_not_crash() -> None:
    import threading

    outcome: list[object] = []

    def run() -> None:
        try:
            with workspace_move_app._signals_ignored():
                outcome.append(signal.getsignal(signal.SIGTERM) != signal.SIG_IGN)
        except BaseException as exc:  # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join()

    assert outcome == [True]


def test_cross_device_rename_fails_without_copying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shutil

    source, target, root = _forward_setup(tmp_path, monkeypatch)
    real_rename = os.rename

    def rename(src: Path, dst: Path, **kwargs: object) -> None:
        if Path(src) == source:
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        return real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    def no_copy(*args: object, **kwargs: object) -> None:
        raise AssertionError("a cross-device move must never copy")

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(shutil, "copytree", no_copy)
    monkeypatch.setattr(shutil, "move", no_copy)

    assert workspace_move_app.before_boot(state_dir=root) == "failed"

    assert source.is_dir() and not target.exists()
    final = _state_dir(root)
    assert final is not None
    assert final.phase == "failed" and "different volume" in final.error


# ── the cli wiring: the restart hook and the environment fix ─────────


def _exec_restart_capture(
    monkeypatch: pytest.MonkeyPatch, *,
    app_engine_flag: bool,
) -> dict[str, object]:
    """Run `cli._run_server` to its execv with `server_main` exiting 75."""
    from ciao import cli

    class _ExecSentinel(Exception):
        pass

    captured: dict[str, object] = {}

    def fake_server_main(**kwargs: object) -> None:
        raise SystemExit(75)

    def fake_execv(path: object, argv: object) -> None:
        captured["env"] = dict(os.environ)
        raise _ExecSentinel()

    reset_exported_dotenv()
    monkeypatch.setenv("CIAO_WORKSPACE", "/ws/here")
    monkeypatch.setenv("CIAO_RUNTIME_ROOT", "/ws/here/.runtime")
    monkeypatch.setattr("ciao.main.main", fake_server_main)
    monkeypatch.setattr(os, "execv", fake_execv)
    with pytest.raises(_ExecSentinel):
        cli._run_server(app_engine=app_engine_flag)
    return captured


def test_app_restart_drops_workspace_env_before_exec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _exec_restart_capture(monkeypatch, app_engine_flag=True)
    env = captured["env"]
    assert isinstance(env, dict)
    # The pointer, read only by the fresh process, is the app-mode boot's
    # workspace source: an exported CIAO_WORKSPACE would pin the old folder.
    assert "CIAO_WORKSPACE" not in env
    assert "CIAO_RUNTIME_ROOT" not in env


def test_legacy_restart_keeps_env(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _exec_restart_capture(monkeypatch, app_engine_flag=False)
    env = captured["env"]
    assert isinstance(env, dict)
    # Without app mode the exec restart keeps the exported source, exactly as
    # before: the environment is still what names the workspace.
    assert env["CIAO_WORKSPACE"] == "/ws/here"


def test_app_run_calls_before_boot_before_server_main(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ciao import cli, workspace_move_app

    order: list[str] = []
    monkeypatch.setattr(
        workspace_move_app,
        "before_boot",
        lambda **kwargs: (order.append("before_boot"), "none")[1],
    )
    monkeypatch.setattr(
        "ciao.main.main", lambda **kwargs: (order.append("server_main"), None)[1]
    )

    assert cli._run_server(app_engine=True) == 0

    # The move (or its rollback) happens before any configuration loads.
    assert order == ["before_boot", "server_main"]
