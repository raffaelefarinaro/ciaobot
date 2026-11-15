from concurrent.futures import ThreadPoolExecutor
import asyncio
from pathlib import Path
from threading import Barrier, Event

import pytest
from starlette.testclient import TestClient

from ciao.config import CiaoConfig, WorkspaceConfig
from ciao.web.app import create_app
from ciao.web.auth import SESSION_COOKIE
from ciao.web.proposal_reservations import ReservationRegistry, validate_scope


def _init_kill_seam(directory, seam, ready, hold):
    import ciao.os_support.locks as locks
    import ciao.os_support.private as private
    import ciao.web.proposal_reservations as reservations

    def pause():
        ready.set()
        hold.wait(30)

    if seam == "parent":
        original = locks.lock_exclusive
        def lock(fd, **kwargs):
            original(fd, **kwargs)
            pause()
        locks.lock_exclusive = lock
    elif seam == "staging":
        original_mkdir = reservations.tempfile.mkdtemp
        def mkdir(*args, **kwargs):
            result = original_mkdir(*args, **kwargs)
            pause()
            return result
        reservations.tempfile.mkdtemp = mkdir
    elif seam == "database":
        original_open = private.open_private
        def open_file(path, *args, **kwargs):
            fd = original_open(path, *args, **kwargs)
            if Path(path).name == "reservations.sqlite3":
                pause()
            return fd
        private.open_private = open_file
    elif seam == "schema":
        original_init = reservations.DurableReservationRegistry._initialize_database
        def initialize(self, capacity):
            original_init(self, capacity)
            pause()
        reservations.DurableReservationRegistry._initialize_database = initialize
    else:
        original_publish = reservations._publish_directory
        def publish(source, target):
            if seam == "publish":
                pause()
            original_publish(source, target)
            if seam == "published":
                pause()
        reservations._publish_directory = publish
    reservations.DurableReservationRegistry(Path(directory))


def _concurrent_initializer(directory, barrier, results):
    from ciao.web.proposal_reservations import DurableReservationRegistry
    barrier.wait()
    registry = DurableReservationRegistry(Path(directory))
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    results.put(token)


@pytest.mark.parametrize("seam", ["parent", "staging", "database", "schema", "publish", "published"])
def test_sqlite_initialization_survives_process_kill(tmp_path: Path, seam: str) -> None:
    import multiprocessing
    import os
    import signal
    from ciao.web.proposal_reservations import DurableReservationRegistry
    context = multiprocessing.get_context("spawn")
    ready, hold = context.Event(), context.Event()
    directory = tmp_path / "reservations"
    child = context.Process(target=_init_kill_seam, args=(str(directory), seam, ready, hold))
    child.start()
    try:
        assert ready.wait(30)
        child.kill()
        child.join(10)
        assert not child.is_alive()
        if os.name != "nt":
            assert child.exitcode == -signal.SIGKILL
        assert directory.exists() == (seam == "published")
        abandoned = list(tmp_path.glob(".reservations.staging-*"))
        registry = DurableReservationRegistry(directory)
        assert all(path.exists() for path in abandoned)
        scope = validate_scope(SCOPE, {"personal"})
        token = registry.reserve(scope)
        assert registry.seal(token, scope)
        assert DurableReservationRegistry(directory).seal(token, scope)
    finally:
        if child.is_alive():
            child.kill()
            child.join(10)


def test_sqlite_concurrent_first_initializers_share_store(tmp_path: Path) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(4), context.Queue()
    directory = tmp_path / "reservations"
    children = [context.Process(target=_concurrent_initializer, args=(str(directory), barrier, results))
                for _ in range(4)]
    for child in children:
        child.start()
    try:
        tokens = [results.get(timeout=30) for _ in children]
        for child in children:
            child.join(10)
            assert child.exitcode == 0
        registry = DurableReservationRegistry(directory)
        scope = validate_scope(SCOPE, {"personal"})
        assert len(set(tokens)) == 4
        assert all(registry.seal(token, scope) for token in tokens)
    finally:
        for child in children:
            if child.is_alive():
                child.kill()
                child.join(10)


SCOPE = {"kind": "row_accept", "workspace": "personal", "targets": ["abc123"]}


def _sqlite_process_race(directory, token, action, barrier, results, capacity=8192) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(Path(directory), capacity=capacity)
    scope = validate_scope(SCOPE, {"personal"})
    barrier.wait(timeout=15)
    try:
        result = (registry.admit(token, scope) if action == "admit"
                  else registry.seal(token, scope) if action == "seal" else bool(registry.reserve(scope)))
    except OverflowError:
        result = False
    results.put((action, result))


def _sqlite_seal_then_wait(directory, token, ready, hold) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(Path(directory))
    assert registry.seal(token, validate_scope(SCOPE, {"personal"}))
    ready.set()
    hold.wait(30)


def _sqlite_complete_then_seal_wait(directory, token, ready, hold) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(Path(directory))
    scope = validate_scope(SCOPE, {"personal"})
    async def operation():
        return await asyncio.to_thread(lambda: "worker completed")
    assert asyncio.run(registry.run(token, scope, operation)) == "worker completed"
    assert registry.seal(token, scope, inspected=True)
    ready.set()
    hold.wait(30)


@pytest.mark.parametrize("actions", [("admit", "admit"), ("admit", "seal")])
def test_sqlite_core_independent_process_transition_race(tmp_path: Path, actions: tuple[str, str]) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_sqlite_process_race, args=(str(path), token, action, barrier, results))
                 for action in actions]
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=30), results.get(timeout=30)]
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert sum(result for _, result in outcomes) == 1
        reopened = DurableReservationRegistry(path)
        if any(action == "admit" and result for action, result in outcomes):
            assert not reopened.finish(token)
            assert not reopened.seal(token, scope, inspected=True)
        else:
            assert reopened.seal(token, scope, inspected=True)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()


def test_sqlite_core_process_admission_vs_reclaim(tmp_path: Path) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path, capacity=1)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_sqlite_process_race, args=(str(path), token, action, barrier, results, 1))
                 for action in ("admit", "reserve")]
    try:
        for process in processes:
            process.start()
        outcomes = dict(results.get(timeout=30) for _ in processes)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert outcomes["admit"] != outcomes["reserve"]
        reopened = DurableReservationRegistry(path, capacity=1)
        assert not reopened.admit(token, scope)
        assert not reopened.seal(token, scope, inspected=True)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()


def test_sqlite_core_process_seal_vs_reclaim(tmp_path: Path) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path, capacity=1)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    context = multiprocessing.get_context("spawn")
    barrier, results = context.Barrier(2), context.Queue()
    processes = [context.Process(target=_sqlite_process_race, args=(str(path), token, action, barrier, results, 1))
                 for action in ("seal", "reserve")]
    try:
        for process in processes:
            process.start()
        outcomes = dict(results.get(timeout=30) for _ in processes)
        for process in processes:
            process.join(timeout=10)
            assert process.exitcode == 0
        assert outcomes["seal"] != outcomes["reserve"]
        reopened = DurableReservationRegistry(path, capacity=1)
        assert reopened.seal(token, scope, inspected=True) == outcomes["seal"]
        assert not reopened.admit(token, scope)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=10)
        results.close()


def test_sqlite_core_seal_commit_survives_process_termination(tmp_path: Path) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    context = multiprocessing.get_context("spawn")
    ready, hold = context.Event(), context.Event()
    process = context.Process(target=_sqlite_seal_then_wait, args=(str(path), token, ready, hold))
    process.start()
    try:
        assert ready.wait(30)
        process.terminate()
        process.join(timeout=10)
        assert process.exitcode is not None
        assert DurableReservationRegistry(path).seal(token, scope, inspected=True)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


def test_sqlite_completed_worker_seal_survives_process_termination(tmp_path: Path) -> None:
    import multiprocessing
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    context = multiprocessing.get_context("spawn")
    ready, hold = context.Event(), context.Event()
    process = context.Process(target=_sqlite_complete_then_seal_wait, args=(str(path), token, ready, hold))
    process.start()
    try:
        assert ready.wait(30)
        process.terminate()
        process.join(timeout=10)
        assert process.exitcode is not None
        reopened = DurableReservationRegistry(path)
        assert reopened.seal(token, scope, inspected=True)
        assert not reopened.admit(token, scope)
    finally:
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


def test_sqlite_core_failed_seal_commit_is_not_proof(tmp_path: Path, monkeypatch) -> None:
    import sqlite3
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    assert registry.admit(token, scope) and registry.finish(token)
    connect = sqlite3.connect
    class FullDisk:
        def __init__(self, conn):
            self.conn = conn
        def __getattr__(self, name):
            return getattr(self.conn, name)
        def commit(self):
            raise sqlite3.OperationalError("database or disk is full")
    with monkeypatch.context() as patch:
        patch.setattr(sqlite3, "connect", lambda *args, **kwargs: FullDisk(connect(*args, **kwargs)))
        with pytest.raises(sqlite3.OperationalError):
            registry.seal(token, scope, inspected=True)
    reopened = DurableReservationRegistry(path)
    assert not reopened.seal(token, scope, inspected=False)
    assert reopened.seal(token, scope, inspected=True)


def test_sqlite_real_full_error_does_not_create_or_erase_proof(tmp_path: Path) -> None:
    import sqlite3
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(tmp_path / "reservations")
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    assert registry.seal(token, scope)
    with registry._connection() as conn:
        pages = conn.execute("PRAGMA page_count").fetchone()[0]
        conn.execute(f"PRAGMA max_page_count={pages}")
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError, match="full"):
            conn.execute("INSERT INTO reservations (token, scope, guard, owner, state, proof_expires) "
                         "VALUES (zeroblob(1048576), zeroblob(32), zeroblob(32), NULL, 'sealed', 1)")
        conn.rollback()
    assert registry.seal(token, scope, inspected=True)


def test_sqlite_live_rollback_journal_is_private_and_contains_no_raw_scope(tmp_path: Path) -> None:
    from ciao.os_support.private import is_private_strict
    from ciao.web.proposal_reservations import DurableReservationRegistry
    directory = tmp_path / "reservations"
    registry = DurableReservationRegistry(directory)
    scope = validate_scope({"kind": "row_accept", "workspace": "private-workspace", "targets": ["sensitive-target"]},
                           {"private-workspace"})
    token = registry.reserve(scope)
    key, _scope_key = registry._keys(token, scope)
    journal = directory / "reservations.sqlite3-journal"
    with registry._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE reservations SET state='active' WHERE token=?", (key,))
        assert journal.exists() and is_private_strict(journal)
        for path in (registry.database, journal):
            content = path.read_bytes()
            assert token.encode() not in content
            assert b"private-workspace" not in content
            assert b"sensitive-target" not in content
        assert not (directory / "reservations.sqlite3-wal").exists()
        assert not (directory / "reservations.sqlite3-shm").exists()
        conn.rollback()
    assert registry.admit(token, scope)


def test_sqlite_core_admission_owner_and_restart(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    first = DurableReservationRegistry(path, capacity=8)
    second = DurableReservationRegistry(path, capacity=8)
    scope = validate_scope(SCOPE, {"personal"})
    wrong = validate_scope({**SCOPE, "targets": ["other"]}, {"personal"})
    token = first.reserve(scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda registry: registry.admit(token, scope), [first, second]))
    assert results.count(True) == 1
    owner, stale = (first, second) if results[0] else (second, first)
    assert owner._owners[token] not in (path / "reservations.sqlite3").read_bytes()
    restarted = DurableReservationRegistry(path, capacity=8)
    assert not restarted.finish(token)
    assert not restarted.seal(token, scope, inspected=True)
    assert not stale.finish(token)
    assert not stale.seal(token, scope, inspected=True)
    assert owner.finish(token)
    assert not owner.seal(token, wrong, inspected=True)
    assert owner.seal(token, scope, inspected=True)
    reopened = DurableReservationRegistry(path, capacity=8)
    assert reopened.seal(token, scope, inspected=True)
    assert not reopened.seal(token, wrong, inspected=True)
    assert not reopened.admit(token, scope)
    assert not reopened.seal("0" * 32 + "." + "1" * 32, scope, inspected=True)
    assert token.encode() not in (path / "reservations.sqlite3").read_bytes()
    assert b"abc123" not in (path / "reservations.sqlite3").read_bytes()
    assert b"personal" not in (path / "reservations.sqlite3").read_bytes()


@pytest.mark.skipif(__import__("os").name != "nt", reason="requires native Windows inherited DACLs")
def test_windows_live_inherited_journal_allows_verified_registry_reopen(tmp_path: Path) -> None:
    from ciao.os_support.private import is_private_strict
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(tmp_path / "reservations")
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    key, _ = registry._keys(token, scope)
    journal = registry.directory / "reservations.sqlite3-journal"
    with registry._connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE reservations SET state='active' WHERE token=?", (key,))
        assert journal.exists()
        assert is_private_strict(journal)
        with registry._connection():
            pass
        conn.rollback()


def test_sqlite_core_refuses_unsafe_or_corrupt_state(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    import os
    import sqlite3
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path, capacity=2)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    sidecar = path / "reservations.sqlite3-wal"
    sidecar.symlink_to(tmp_path / "elsewhere")
    with pytest.raises((OSError, ValueError)):
        registry.admit(token, scope)
    sidecar.unlink()
    database = path / "reservations.sqlite3"
    database.write_bytes(b"corrupt")
    os.chmod(database, 0o600)
    with pytest.raises((sqlite3.DatabaseError, ValueError)):
        DurableReservationRegistry(path, capacity=2)
    assert database.read_bytes() == b"corrupt"


def test_sqlite_existing_missing_database_is_not_recreated(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    directory = tmp_path / "reservations"
    registry = DurableReservationRegistry(directory)
    registry.database.unlink()
    with pytest.raises(FileNotFoundError):
        DurableReservationRegistry(directory)
    assert not registry.database.exists()


def test_sqlite_publication_never_replaces_existing_empty_directory(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import _publish_directory
    source, target = tmp_path / "source", tmp_path / "target"
    source.mkdir()
    target.mkdir()
    with pytest.raises(OSError):
        _publish_directory(source, target)
    assert source.is_dir() and target.is_dir()


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX permission bits")
def test_sqlite_core_refuses_existing_nonprivate_directory_without_chmod(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    import os
    import stat
    path = tmp_path / "unsafe"
    path.mkdir(mode=0o755)
    os.chmod(path, 0o755)
    with pytest.raises(PermissionError):
        DurableReservationRegistry(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert not (path / "reservations.sqlite3").exists()


def test_sqlite_pressure_reclaims_only_oldest_reserved_across_reopen(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path, capacity=4)
    scope = validate_scope(SCOPE, {"personal"})
    active, finished, sealed, abandoned = (registry.reserve(scope) for _ in range(4))
    assert registry.admit(active, scope)
    assert registry.admit(finished, scope) and registry.finish(finished)
    assert registry.seal(sealed, scope)
    for _ in range(520):
        previous = abandoned
        registry = DurableReservationRegistry(path, capacity=4)
        abandoned = registry.reserve(scope)
        assert not registry.admit(previous, scope)
        assert not registry.seal(previous, scope, inspected=True)
    assert not registry.seal(active, scope, inspected=True)
    assert registry.seal(finished, scope, inspected=True)
    assert registry.seal(sealed, scope, inspected=True)
    assert registry.admit(abandoned, scope)
    with pytest.raises(OverflowError):
        registry.reserve(scope)


def test_durable_proof_retention_expiry_and_clock_rollback(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry, _PROOF_RETENTION_SECONDS
    now = [1000.0]
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path, capacity=2, clock=lambda: now[0])
    scope = validate_scope(SCOPE, {"personal"})
    first = registry.reserve(scope)
    assert registry.seal(first, scope)
    active = registry.reserve(scope)
    assert registry.admit(active, scope)
    now[0] += _PROOF_RETENTION_SECONDS - 1
    with pytest.raises(OverflowError):
        registry.reserve(scope)
    now[0] -= 100
    reopened = DurableReservationRegistry(path, capacity=2, clock=lambda: now[0])
    with pytest.raises(OverflowError):
        reopened.reserve(scope)
    assert reopened.seal(first, scope, inspected=True)
    now[0] = 1000.0 + _PROOF_RETENTION_SECONDS
    fresh = reopened.reserve(scope)
    assert not reopened.seal(first, scope, inspected=True)
    assert not reopened.admit(first, scope)
    assert not reopened.seal(active, scope, inspected=True)
    assert reopened.admit(fresh, scope)


def test_durable_default_capacity_exceeds_512_normal_seals(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(tmp_path / "reservations")
    scope = validate_scope(SCOPE, {"personal"})
    tokens = []
    for _ in range(513):
        token = registry.reserve(scope)
        assert registry.admit(token, scope)
        assert registry.finish(token)
        assert registry.seal(token, scope, inspected=True)
        tokens.append(token)
    assert registry.seal(tokens[0], scope, inspected=True)


def test_durable_expiry_races_seal_and_rejects_scope_mismatch(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry, _PROOF_RETENTION_SECONDS
    now = [100.0]
    path = tmp_path / "reservations"
    first = DurableReservationRegistry(path, capacity=1, clock=lambda: now[0])
    second = DurableReservationRegistry(path, capacity=1, clock=lambda: now[0])
    scope = validate_scope(SCOPE, {"personal"})
    wrong = validate_scope({**SCOPE, "targets": ["other"]}, {"personal"})
    token = first.reserve(scope)
    assert first.seal(token, scope)
    now[0] += _PROOF_RETENTION_SECONDS - 1
    assert not second.seal(token, wrong, inspected=True)
    assert second.seal(token, scope, inspected=True)
    now[0] += 1
    barrier = Barrier(2)
    def seal():
        barrier.wait()
        return second.seal(token, scope, inspected=True)
    def reserve():
        barrier.wait()
        return first.reserve(scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        sealing, replacement = pool.submit(seal), pool.submit(reserve)
        assert not sealing.result()
        assert replacement.result() != token
    assert not DurableReservationRegistry(path, capacity=1, clock=lambda: now[0]).seal(token, scope, inspected=True)


def test_sqlite_reclaim_does_not_depend_on_wall_clock(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    now = [10_000.0]
    path = tmp_path / "reservations"
    scope = validate_scope(SCOPE, {"personal"})
    first = DurableReservationRegistry(path, capacity=2, clock=lambda: now[0])
    older = first.reserve(scope)
    now[0] = -10_000.0
    second = DurableReservationRegistry(path, capacity=2, clock=lambda: now[0])
    newer = second.reserve(scope)
    fresh = second.reserve(scope)
    assert not second.admit(older, scope)
    assert second.admit(newer, scope)
    assert second.admit(fresh, scope)


def test_sqlite_failed_replacement_does_not_reclaim_existing_reservation(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(tmp_path / "reservations", capacity=1)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    invalid_sweep = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-01-01"],
                                    "workspaces": ["personal"]}, {"personal"})
    with pytest.raises(ValueError, match="snapshot unavailable"):
        registry.reserve(invalid_sweep)
    assert registry.admit(token, scope)


def test_sqlite_core_capacity_reclaim_and_storage_failure(tmp_path: Path, monkeypatch) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    import sqlite3
    registry = DurableReservationRegistry(tmp_path / "reservations", capacity=2)
    scope = validate_scope(SCOPE, {"personal"})
    abandoned = registry.reserve(scope)
    active = registry.reserve(scope)
    assert registry.admit(active, scope)
    fresh = registry.reserve(scope)
    assert not registry.admit(abandoned, scope)
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("injected disk failure")
        patch.setattr(sqlite3, "connect", fail)
        with pytest.raises(sqlite3.OperationalError):
            registry.admit(fresh, scope)
    assert registry.admit(fresh, scope)
    assert not registry.seal(active, scope, inspected=True)
    assert registry.finish(active) and registry.seal(active, scope, inspected=True)
    assert registry.finish(fresh) and registry.seal(fresh, scope, inspected=True)
    with pytest.raises(OverflowError):
        registry.reserve(scope)


def test_sqlite_reclaim_admission_seal_race_rejects_delayed_dispatch(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    first = DurableReservationRegistry(path, capacity=1)
    second = DurableReservationRegistry(path, capacity=1)
    scope = validate_scope(SCOPE, {"personal"})
    token = first.reserve(scope)
    barrier = Barrier(2)
    def reclaim():
        barrier.wait()
        return first.reserve(scope)
    def seal():
        barrier.wait()
        return second.seal(token, scope, inspected=True)
    with ThreadPoolExecutor(max_workers=2) as pool:
        reclaimed, sealing = pool.submit(reclaim), pool.submit(seal)
        sealed = sealing.result()
        if sealed:
            with pytest.raises(OverflowError):
                reclaimed.result()
        else:
            assert reclaimed.result() != token
    assert not first.admit(token, scope)


def test_sqlite_sweep_binds_server_snapshot_but_seals_with_public_scope(tmp_path: Path) -> None:
    from dataclasses import replace
    from ciao.web.proposal_reservations import DurableReservationRegistry
    registry = DurableReservationRegistry(tmp_path / "reservations")
    public = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-01-01"],
                             "workspaces": ["personal"]}, {"personal"})
    original = replace(public, sweep_snapshot=(("personal", "/vault-a", "/vault-a/Workspace/Memory-Proposals.md", 1, 2),))
    changed = replace(public, sweep_snapshot=(("personal", "/vault-b", "/vault-b/Workspace/Memory-Proposals.md", 1, 3),))
    with pytest.raises(ValueError, match="snapshot unavailable"):
        registry.reserve(public)
    token = registry.reserve(original)
    assert not registry.admit(token, changed)
    assert registry.admit(token, original)
    assert registry.finish(token)
    different = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-01-02"],
                                "workspaces": ["personal"]}, {"personal"})
    assert not registry.seal(token, different, inspected=True)
    assert registry.seal(token, public, inspected=True)
    assert DurableReservationRegistry(tmp_path / "reservations").seal(token, public, inspected=True)
    assert b"/vault-a" not in (tmp_path / "reservations" / "reservations.sqlite3").read_bytes()


@pytest.mark.asyncio
async def test_sqlite_sweep_retains_server_snapshot_and_rejects_changed_root(tmp_path: Path) -> None:
    from dataclasses import replace
    from ciao.web.proposal_reservations import DurableReservationRegistry, ReservationAdmissionError
    registry = DurableReservationRegistry(tmp_path / "reservations")
    public = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-01-01"],
                             "workspaces": ["personal"]}, {"personal"})
    original = (("personal", "/vault-a", "/vault-a/Workspace/Memory-Proposals.md", 1, 2),)
    changed = (("personal", "/vault-b", "/vault-b/Workspace/Memory-Proposals.md", 1, 3),)
    token = registry.reserve(replace(public, sweep_snapshot=original))
    calls = []
    async def operation(scope):
        calls.append(scope.sweep_snapshot)
        return "done"
    with pytest.raises(ReservationAdmissionError):
        await registry.run_sweep(token, "2026-01-01", operation, lambda: changed)
    assert not calls
    assert await registry.run_sweep(token, "2026-01-01", operation, lambda: original) == "done"
    assert calls == [original]
    assert registry.seal(token, public, inspected=True)
    with pytest.raises(ReservationAdmissionError):
        await registry.run_sweep(token, "2026-01-01", operation, lambda: (_ for _ in ()).throw(AssertionError()))


def test_sqlite_core_admit_and_seal_race(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    first = DurableReservationRegistry(path)
    second = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = first.reserve(scope)
    barrier = Barrier(2)
    def admit():
        barrier.wait()
        return first.admit(token, scope)
    def seal():
        barrier.wait()
        return second.seal(token, scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        admission = pool.submit(admit)
        sealing = pool.submit(seal)
        admitted, sealed = admission.result(), sealing.result()
    assert admitted != sealed
    if admitted:
        assert not second.seal(token, scope, inspected=True)
        assert first.finish(token)
        assert second.seal(token, scope, inspected=True)
    assert DurableReservationRegistry(path).seal(token, scope, inspected=True)


@pytest.mark.asyncio
async def test_sqlite_core_cancelled_request_retains_worker_and_finishes(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    path = tmp_path / "reservations"
    registry = DurableReservationRegistry(path)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    entered, release = Event(), Event()
    def worker() -> str:
        entered.set()
        assert release.wait(10)
        return "complete"
    async def operation() -> str:
        return await asyncio.to_thread(worker)
    task = asyncio.create_task(registry.run(token, scope, operation))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        reopened = DurableReservationRegistry(path)
        assert not reopened.finish(token)
        assert not reopened.seal(token, scope, inspected=True)
    finally:
        release.set()
    await asyncio.gather(*tuple(registry._tasks))
    assert DurableReservationRegistry(path).seal(token, scope, inspected=True)


def test_reservation_state_machine_and_restart() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    assert registry.admit(token, scope)
    assert not registry.admit(token, scope)
    assert not registry.seal(token, scope)
    assert registry.finish(token)
    assert not registry.seal(token, scope)
    assert registry.seal(token, scope, inspected=True)
    assert registry.seal(token, scope, inspected=True)
    assert not registry.admit(token, scope)
    assert not ReservationRegistry().seal(token, scope)


def test_unsealed_finished_batch_survives_other_clients_sealed_lru_eviction() -> None:
    registry = ReservationRegistry(capacity=2, sealed_capacity=1)
    batch = validate_scope({"kind": "batch_accept", "workspace": "", "targets": ["a", "b"],
                            "workspaces": ["personal"]}, {"personal"})
    other = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(batch)
    assert registry.admit(token, batch) and registry.finish(token)
    for _ in range(3):
        other_token = registry.reserve(other)
        assert registry.seal(other_token, other)
    assert registry.seal(token, batch, inspected=True)
    assert registry.seal(token, batch, inspected=True)


def test_seal_before_dispatch_and_scope_mismatch() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    other = validate_scope({**SCOPE, "targets": ["other"]}, {"personal"})
    token = registry.reserve(scope)
    assert not registry.seal(token, other)
    assert not registry.admit(token, other)
    assert registry.seal(token, scope)
    assert not registry.admit(token, scope)


def test_competing_admissions_have_one_winner() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(lambda _: registry.admit(token, scope), range(40)))
    assert results.count(True) == 1


@pytest.mark.asyncio
async def test_cancelled_request_retains_active_work_until_thread_exits() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    entered, release = Event(), Event()

    def worker() -> str:
        entered.set()
        assert release.wait(5)
        return "done"

    async def operation() -> str:
        return await asyncio.to_thread(worker)

    request = asyncio.create_task(registry.run(token, scope, operation))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not registry.seal(token, scope, inspected=True)
        assert not registry.admit(token, scope)
    finally:
        release.set()
    # The operation-owned task is retained until its thread returns.
    await asyncio.gather(*tuple(registry._tasks))
    await asyncio.sleep(0)
    assert registry.seal(token, scope, inspected=True)


def test_seal_and_dispatch_race_has_only_one_winner() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    for _ in range(40):
        token = registry.reserve(scope)
        barrier = Barrier(2)
        def admit() -> bool:
            barrier.wait()
            return registry.admit(token, scope)
        def seal() -> bool:
            barrier.wait()
            return registry.seal(token, scope)
        with ThreadPoolExecutor(max_workers=2) as pool:
            admission = pool.submit(admit)
            sealing = pool.submit(seal)
            assert admission.result() != sealing.result()


def test_active_entry_cannot_be_finished_or_sealed_by_unknown_id() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    assert registry.admit(token, scope)
    assert not registry.finish("unknown")
    assert not registry.seal(token, scope)
    assert not ReservationRegistry().seal(token, scope)
    assert registry.finish(token)
    assert not registry.finish(token)
    assert registry.seal(token, scope, inspected=True)


def test_registry_capacity_never_evicts_unsealed_evidence() -> None:
    registry = ReservationRegistry(capacity=1)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    with pytest.raises(OverflowError):
        registry.reserve(scope)
    assert registry.seal(token, scope)
    assert registry.reserve(scope)


def test_more_than_capacity_sequential_completed_operations_remain_live() -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    for _ in range(1100):
        token = registry.reserve(scope)
        assert registry.admit(token, scope)
        assert registry.finish(token)
        assert registry.seal(token, scope, inspected=True)
        assert registry.seal(token, scope, inspected=True)
        assert not registry.admit(token, scope)


@pytest.mark.parametrize("state", ["reserved", "active", "finished"])
def test_512_unsealed_operations_remain_fail_closed(state: str) -> None:
    registry = ReservationRegistry()
    scope = validate_scope(SCOPE, {"personal"})
    tokens = [registry.reserve(scope) for _ in range(512)]
    if state != "reserved":
        for token in tokens:
            assert registry.admit(token, scope)
            if state == "finished":
                assert registry.finish(token)
    with pytest.raises(OverflowError):
        registry.reserve(scope)
    if state == "active":
        assert not registry.seal(tokens[0], scope, inspected=True)
    if state == "finished":
        assert not registry.seal(tokens[0], scope)


def test_old_sealed_id_is_unknown_and_never_readmitted_after_eviction() -> None:
    registry = ReservationRegistry(sealed_capacity=2)
    scope = validate_scope(SCOPE, {"personal"})
    tokens = [registry.reserve(scope) for _ in range(3)]
    for token in tokens:
        assert registry.seal(token, scope)
    assert not registry.seal(tokens[0], scope, inspected=True)
    assert not registry.admit(tokens[0], scope)
    assert registry.seal(tokens[1], scope)
    assert registry.seal(tokens[2], scope)
    assert not ReservationRegistry().seal(tokens[2], scope)


def test_concurrent_sealing_and_reservation_never_discard_live_work() -> None:
    registry = ReservationRegistry(capacity=64, sealed_capacity=8)
    scope = validate_scope(SCOPE, {"personal"})
    active = registry.reserve(scope)
    assert registry.admit(active, scope)

    def cycle(_: int) -> None:
        token = registry.reserve(scope)
        assert registry.admit(token, scope)
        assert registry.finish(token)
        assert registry.seal(token, scope, inspected=True)
        assert not registry.admit(token, scope)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(cycle, range(200)))
    assert not registry.seal(active, scope, inspected=True)
    assert registry.finish(active)
    assert registry.seal(active, scope, inspected=True)


def test_expired_abandoned_reservations_free_capacity_without_admission() -> None:
    now = [0.0]
    registry = ReservationRegistry(clock=lambda: now[0])
    scope = validate_scope(SCOPE, {"personal"})
    tokens = [registry.reserve(scope) for _ in range(512)]
    with pytest.raises(OverflowError):
        registry.reserve(scope)
    now[0] = 301.0
    for token in tokens:
        assert not registry.admit(token, scope)
        assert not registry.seal(token, scope, inspected=True)
    assert len([registry.reserve(scope) for _ in range(512)]) == 512


@pytest.mark.parametrize("state", ["active", "finished"])
def test_expiry_never_discards_admitted_work(state: str) -> None:
    now = [0.0]
    registry = ReservationRegistry(capacity=1, clock=lambda: now[0])
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    assert registry.admit(token, scope)
    if state == "finished":
        assert registry.finish(token)
    now[0] = 10000.0
    with pytest.raises(OverflowError):
        registry.reserve(scope)
    if state == "active":
        assert not registry.seal(token, scope, inspected=True)
        assert registry.finish(token)
    assert registry.seal(token, scope, inspected=True)


@pytest.mark.parametrize("operation", ["admit", "seal"])
def test_expiry_and_transition_are_serialized(operation: str) -> None:
    now = [0.0]
    registry = ReservationRegistry(clock=lambda: now[0])
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    now[0] = 301.0
    barrier = Barrier(2)
    def transition() -> bool:
        barrier.wait()
        return registry.admit(token, scope) if operation == "admit" else registry.seal(token, scope)
    def reclaim() -> str:
        barrier.wait()
        return registry.reserve(scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = pool.submit(transition)
        fresh = pool.submit(reclaim)
        assert not result.result()
        assert fresh.result() != token
    assert not registry.admit(token, scope)


@pytest.mark.asyncio
async def test_expired_sweep_cannot_dispatch() -> None:
    now = [0.0]
    registry = ReservationRegistry(clock=lambda: now[0])
    scope = validate_scope({"kind": "sweep", "workspace": "", "targets": ["2026-01-01"],
                            "workspaces": ["personal"]}, {"personal"})
    token = registry.reserve(scope)
    now[0] = 301.0
    ran = False
    async def operation(_scope):
        nonlocal ran
        ran = True
    from ciao.web.proposal_reservations import ReservationAdmissionError
    with pytest.raises(ReservationAdmissionError):
        await registry.run_sweep(token, "2026-01-01", operation, lambda: ())
    assert not ran and not registry.seal(token, scope)


@pytest.mark.parametrize("operation", ["admit", "seal"])
def test_transition_before_deadline_wins_against_expiry(operation: str) -> None:
    now = [0.0]
    entered, release = Event(), Event()
    armed = [False]
    def clock() -> float:
        value = now[0]
        if armed[0]:
            armed[0] = False
            entered.set()
            assert release.wait(3)
        return value
    registry = ReservationRegistry(capacity=1, clock=clock)
    scope = validate_scope(SCOPE, {"personal"})
    token = registry.reserve(scope)
    now[0] = 299.0
    armed[0] = True
    with ThreadPoolExecutor(max_workers=2) as pool:
        transition = pool.submit(registry.admit if operation == "admit" else registry.seal, token, scope)
        assert entered.wait(3)
        now[0] = 301.0
        competing = pool.submit(registry.reserve, scope)
        release.set()
        assert transition.result()
        if operation == "admit":
            with pytest.raises(OverflowError):
                competing.result()
            assert not registry.seal(token, scope, inspected=True)
            assert registry.finish(token)
            assert registry.seal(token, scope, inspected=True)
        else:
            assert competing.result() != token
            assert not registry.admit(token, scope)


@pytest.mark.parametrize("scope", [
    {**SCOPE, "kind": "wrong"}, {**SCOPE, "workspace": "other"},
    {**SCOPE, "targets": ["../path"]}, {**SCOPE, "targets": []},
    {**SCOPE, "extra": 1}, {**SCOPE, "targets": ["abc", "abc"]},
    {**SCOPE, "destination": None}, {**SCOPE, "destination": "../path"},
    {"kind": "sweep", "workspace": "", "targets": ["2026-02-31"], "workspaces": ["personal"]},
    {"kind": "sweep", "workspace": "personal", "targets": ["2026-01-01"], "workspaces": ["personal"]},
])
def test_scope_validation_rejects_invalid(scope: dict) -> None:
    with pytest.raises(ValueError):
        validate_scope(scope, {"personal"})


@pytest.mark.parametrize("scope", [
    {"kind": "row_dismiss", "workspace": "", "targets": ["abc"]},
    {"kind": "implement", "workspace": "personal", "targets": ["abc"]},
    {"kind": "batch_accept", "workspace": "", "targets": ["abc", "def"], "workspaces": ["personal"]},
    {"kind": "batch_dismiss", "workspace": "", "targets": ["abc", "def"], "workspaces": ["personal"]},
    {"kind": "sweep", "workspace": "", "targets": ["2026-01-01"], "workspaces": ["personal"]},
    {"kind": "undo", "workspace": "", "targets": ["mrcpt_1"]},
])
def test_valid_scope_kinds(scope: dict) -> None:
    assert validate_scope(scope, {"personal"}).kind == scope["kind"]


def test_app_durable_seal_survives_reopen_without_trusting_unknown(tmp_path: Path) -> None:
    from ciao.web.proposal_reservations import DurableReservationRegistry
    config = CiaoConfig(
        pwa_auth_token="test-secret", workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json", media_root=tmp_path / "media",
        vault_root=tmp_path / "vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="vault/personal")},
    )
    app = create_app(config)
    assert isinstance(app.state.proposal_reservations, DurableReservationRegistry)
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    headers = {"Origin": "https://ciao.example"}
    response = client.post("/api/proposals/reservations", json=SCOPE, headers=headers)
    assert response.status_code == 200
    token = response.json()["id"]
    scope = validate_scope(SCOPE, {"personal"})
    assert app.state.proposal_reservations.admit(token, scope)
    assert app.state.proposal_reservations.finish(token)
    body = {"id": token, "scope": SCOPE, "inspected": True}
    assert client.post("/api/proposals/reservations/seal", json=body, headers=headers).status_code == 200
    other_app = create_app(config)
    other = TestClient(other_app, base_url="https://ciao.example")
    other.cookies.set(SESSION_COOKIE, other_app.state.serializer.dumps({"user": "owner"}))
    assert other.post("/api/proposals/reservations/seal", json=body, headers=headers).status_code == 200
    assert other.post("/api/proposals/reservations/seal",
                      json={**body, "id": "0" * 32 + "." + "1" * 32}, headers=headers).status_code == 409


def test_app_does_not_fallback_if_durable_database_disappears(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-secret", workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json", media_root=tmp_path / "media",
        vault_root=tmp_path / "vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="vault/personal")},
    )
    app = create_app(config)
    app.state.proposal_reservations.database.unlink()
    with pytest.raises(FileNotFoundError):
        create_app(config)


def test_routes_require_session_and_enforce_scope(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-secret", workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json", media_root=tmp_path / "media",
        vault_root=tmp_path / "vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="vault/personal")},
    )
    app = create_app(config, reservation_registry=ReservationRegistry())
    client = TestClient(app, base_url="https://ciao.example")
    headers = {"Origin": "https://ciao.example"}
    assert client.post("/api/proposals/reservations", json=SCOPE, headers=headers).status_code == 401
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    assert client.post("/api/proposals/reservations", json=SCOPE,
                       headers={"Origin": "https://attacker.example"}).status_code == 403
    response = client.post("/api/proposals/reservations", json=SCOPE, headers=headers)
    assert response.status_code == 200
    token = response.json()["id"]
    seal = {"id": token, "scope": SCOPE, "inspected": False}
    assert client.post("/api/proposals/reservations/seal", json={**seal, "scope": {**SCOPE, "targets": ["other"]}}, headers=headers).status_code == 409
    assert client.post("/api/proposals/reservations/seal", json=seal, headers=headers).status_code == 200
    assert not app.state.proposal_reservations.admit(token, validate_scope(SCOPE, {"personal"}))
    assert client.post("/api/proposals/reservations/seal", json=seal, headers=headers).status_code == 200
    assert client.post("/api/proposals/reservations/seal", json={"id": "../invalid", "scope": SCOPE, "inspected": False}, headers=headers).status_code == 400
    assert client.post("/api/proposals/reservations", content="{", headers={**headers, "Content-Type": "application/json"}).status_code == 400
    sweep = client.post("/api/proposals/reservations", json={"kind": "sweep", "workspace": "", "targets": ["2026-01-01"]}, headers=headers)
    assert sweep.status_code == 200
    assert sweep.json()["scope"]["workspaces"] == ["personal"]
    active = client.post("/api/proposals/reservations", json=SCOPE, headers=headers).json()["id"]
    assert app.state.proposal_reservations.admit(active, validate_scope(SCOPE, {"personal"}))
    assert client.post("/api/proposals/reservations/seal", json={"id": active, "scope": SCOPE, "inspected": True}, headers=headers).status_code == 409
    assert app.state.proposal_reservations.finish(active)
    assert client.post("/api/proposals/reservations/seal", json={"id": active, "scope": SCOPE, "inspected": False}, headers=headers).status_code == 409
    assert client.post("/api/proposals/reservations/seal", json={"id": active, "scope": SCOPE, "inspected": True}, headers=headers).status_code == 200
    other_app = create_app(config, reservation_registry=ReservationRegistry())
    other_client = TestClient(other_app, base_url="https://ciao.example")
    other_client.cookies.set(SESSION_COOKIE, other_app.state.serializer.dumps({"user": "owner"}))
    assert other_client.post("/api/proposals/reservations/seal", json={"id": active, "scope": SCOPE, "inspected": True}, headers=headers).status_code == 409


def test_seal_accepts_exact_scope_after_workspace_removal_but_not_mismatch(tmp_path: Path) -> None:
    config = CiaoConfig(
        pwa_auth_token="test-secret", workspace_root=tmp_path,
        state_path=tmp_path / ".runtime" / "state.json", media_root=tmp_path / "media",
        vault_root=tmp_path / "vault",
        workspaces={"personal": WorkspaceConfig(name="personal", vault_root="vault/personal"),
                    "work": WorkspaceConfig(name="work", vault_root="vault/work")},
    )
    app = create_app(config)
    client = TestClient(app, base_url="https://ciao.example")
    client.cookies.set(SESSION_COOKIE, app.state.serializer.dumps({"user": "owner"}))
    headers = {"Origin": "https://ciao.example"}
    response = client.post("/api/proposals/reservations", json=SCOPE, headers=headers)
    assert response.status_code == 200
    token = response.json()["id"]
    scope = validate_scope(SCOPE, {"personal", "work"})
    assert app.state.proposal_reservations.admit(token, scope)
    assert app.state.proposal_reservations.finish(token)
    del config.workspaces["personal"]
    assert "personal" not in config.workspace_names()
    body = {"id": token, "scope": SCOPE, "inspected": True}
    assert client.post("/api/proposals/reservations/seal", json=body,
                       headers={"Origin": "https://attacker.example"}).status_code == 403
    assert client.post("/api/proposals/reservations/seal",
                       json={**body, "scope": {**SCOPE, "workspace": "work"}}, headers=headers).status_code == 409
    assert client.post("/api/proposals/reservations/seal", json=body, headers=headers).status_code == 200
    assert client.post("/api/proposals/reservations/seal", json=body, headers=headers).status_code == 200
    assert client.post("/api/proposals/reservations", json=SCOPE, headers=headers).status_code == 400
