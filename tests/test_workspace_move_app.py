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
) -> workspace_move.MoveOperation:
    """The operation record the app-mode flow leaves behind for this boot."""
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
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
        _op(source, target, phase=phase, runtime_root=str(source / ".runtime")),
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
    monkeypatch.setattr(
        workspace_move,
        "rewrite_prefix",
        lambda value, old, new: (_ for _ in ()).throw(RuntimeError("prefix blew up")),
    )
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
    assert result == "skipped"
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
