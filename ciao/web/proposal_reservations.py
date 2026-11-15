"""In-process, single-use proposal operation reservations.

This registry does not itself protect mutations: handlers must admit before
effects and finish only after all work, including worker threads, has ended.
"""

from __future__ import annotations

import re
import secrets
import threading
import asyncio
import time
import hashlib
import json
import math
import os
import sqlite3
import stat
import sys
import tempfile
from contextlib import contextmanager
from collections import OrderedDict
from collections.abc import Callable, Coroutine
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path
from typing import Any, Iterator, TypeVar

from starlette.requests import Request
from starlette.responses import JSONResponse

_EPOCH = secrets.token_hex(16)
_TARGET = re.compile(r"^[A-Za-z0-9_:-]{1,256}$")
_KINDS = {"row_accept", "row_dismiss", "implement", "batch_accept", "batch_dismiss", "sweep", "undo"}
_BATCH = {"batch_accept", "batch_dismiss"}
_RESERVED_TTL_SECONDS = 300.0
_PROOF_RETENTION_SECONDS = 30 * 24 * 60 * 60
_DURABLE_CAPACITY = 8192


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish_directory(source: Path, target: Path) -> None:
    """Atomically publish without replacing even an empty existing directory."""
    if os.name == "nt":
        os.rename(source, target)
        return
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    result: int
    if sys.platform == "darwin":
        rename = libc.renamex_np
        rename.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = rename(os.fsencode(source), os.fsencode(target), 4)  # RENAME_EXCL
    elif sys.platform.startswith("linux"):
        rename = libc.renameat2
        rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)  # RENAME_NOREPLACE
    else:
        raise OSError("exclusive directory publication is unavailable")
    if result:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))
_T = TypeVar("_T")


@dataclass(frozen=True)
class ReservationScope:
    kind: str
    workspace: str
    targets: tuple[str, ...]
    workspaces: tuple[str, ...] = ()
    destination: str | None = None
    destinations: tuple[tuple[str, str], ...] = ()
    sweep_snapshot: tuple[tuple[str, str, str, int, int], ...] = field(default=(), compare=False, repr=False)


class ReservationAdmissionError(ValueError):
    """The reservation was not available for this operation."""


def validate_scope(value: Any, workspaces: set[str] | None) -> ReservationScope:
    if not isinstance(value, dict):
        raise ValueError("invalid reservation scope")
    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in _KINDS:
        raise ValueError("invalid reservation kind")
    expected = {"kind", "workspace", "targets", "workspaces"} if kind in _BATCH or kind == "sweep" else {"kind", "workspace", "targets"}
    if kind == "row_accept" and "destination" in value:
        expected = {*expected, "destination"}
    if kind == "batch_accept" and "destinations" in value:
        expected = {*expected, "destinations"}
    if set(value) != expected:
        raise ValueError("invalid reservation scope")
    workspace, targets = value["workspace"], value["targets"]
    destination = value.get("destination")
    if "destination" in value and (not isinstance(destination, str) or not destination or len(destination) > 256
                                    or "\0" in destination or (workspaces is not None and destination not in workspaces)):
        raise ValueError("invalid reservation destination")
    if (not isinstance(workspace, str) or len(workspace) > 256 or "\0" in workspace
            or (workspace and workspaces is not None and workspace not in workspaces)):
        raise ValueError("invalid reservation workspace")
    if not isinstance(targets, list) or len(targets) > 200:
        raise ValueError("invalid reservation targets")
    if any(not isinstance(target, str) or not _TARGET.fullmatch(target) for target in targets):
        raise ValueError("invalid reservation target")
    if len(set(targets)) != len(targets):
        raise ValueError("duplicate reservation target")
    if (kind == "sweep" and (len(targets) != 1 or workspace)) or (kind in _BATCH and not targets):
        raise ValueError("invalid reservation scope")
    if kind == "sweep":
        try:
            if date.fromisoformat(targets[0]).isoformat() != targets[0]:
                raise ValueError("invalid sweep date")
        except ValueError as exc:
            raise ValueError("invalid sweep date") from exc
    if kind != "sweep" and kind not in _BATCH and len(targets) != 1:
        raise ValueError("invalid reservation target count")
    batch_workspaces = value.get("workspaces", [])
    if (not isinstance(batch_workspaces, list) or len(batch_workspaces) > 200
            or any(not isinstance(item, str) or len(item) > 256 or "\0" in item
                   or (item and workspaces is not None and item not in workspaces)
                   for item in batch_workspaces)):
        raise ValueError("invalid reservation workspaces")
    if ((kind in _BATCH and not batch_workspaces)
            or ((kind in _BATCH or kind == "sweep") and batch_workspaces != sorted(set(batch_workspaces)))):
        raise ValueError("invalid reservation workspaces")
    destinations = value.get("destinations", {})
    if (not isinstance(destinations, dict) or len(destinations) > len(targets)
            or any(not isinstance(key, str) or key not in targets or not isinstance(target, str)
                   or not target or len(target) > 256 or "\0" in target
                   or (workspaces is not None and target not in workspaces) for key, target in destinations.items())):
        raise ValueError("invalid reservation destinations")
    return ReservationScope(kind, workspace, tuple(targets), tuple(batch_workspaces), destination,
                            tuple(sorted(destinations.items())))


def batch_destinations(resolved: list[dict[str, Any]], requested: str, workspaces: set[str]) -> dict[str, str]:
    from ciao.web.proposal_service import _rehome_target
    destinations = {}
    for ctx in resolved:
        if ctx.get("workspace") != ctx.get("row", {}).get("workspace"):
            raise ValueError("inconsistent workspace")
        row = ctx["row"]
        if row.get("kind") == "rehome":
            target, error = _rehome_target(row, requested)
            if error or target not in workspaces or target == ctx["workspace"]:
                raise ValueError("invalid re-home destination")
            destinations[row["id"]] = target
    return destinations


def capture_sweep_snapshot(config: Any) -> tuple[tuple[str, str, str, int, int], ...]:
    """Capture registered roots and queue paths; no client path is consulted."""
    from ciao.web.proposal_service import _PROPOSALS_REL
    result = []
    for workspace in sorted(config.workspace_names()):
        vault = Path(config.workspace_vault_root(workspace)).resolve()
        queue = vault.joinpath(*_PROPOSALS_REL).resolve()
        if not queue.is_relative_to(vault):
            raise ValueError("sweep queue escapes vault")
        try:
            info = vault.stat()
        except FileNotFoundError:
            device, inode = -1, -1
        else:
            if not stat.S_ISDIR(info.st_mode) or not info.st_ino:
                raise ValueError("sweep vault is not an identifiable directory")
            device, inode = info.st_dev, info.st_ino
        result.append((workspace, str(vault), str(queue), device, inode))
    return tuple(result)


class DurableReservationRegistry:
    """Bounded durable registry for reservation-backed operations.

    SQLite opens by pathname. The private directory is trusted against same-user,
    root and trusted-ancestor replacement; this is not descriptor-bound SQLite.
    Sealed proofs are retained for 30 days according to the system wall clock.
    macOS extended ACLs are checked; the strict verifier does not inspect Linux
    POSIX ACLs. Unverifiable Windows DACLs or reparse points are refused.
    Capacity pressure reclaims the oldest still-reserved entry or expired sealed
    proof, never active, finished or unexpired sealed entries. A detected clock
    rollback pauses proof expiry; unobserved forward clock jumps cannot be detected.
    Reclaimed IDs are unknown, not completion proof.
    """

    def __init__(self, directory: Path, *, capacity: int = _DURABLE_CAPACITY,
                 clock: Callable[[], float] = time.time) -> None:
        from ciao.os_support.private import is_private_strict, make_private_dir, open_private
        from ciao.os_support.locks import lock_exclusive, unlock
        from ciao.os_support.files import open_fd

        if capacity < 1:
            raise ValueError("invalid reservation capacity")
        self.directory = Path(directory)
        self.database = self.directory / "reservations.sqlite3"
        self.capacity = capacity
        self.clock = clock
        self._owners: dict[str, bytes] = {}
        self._owners_lock = threading.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        final = self.directory
        lock_path = final.parent / f".{final.name}.init.lock"
        lock_fd = open_private(lock_path, os.O_RDWR, follow_symlinks=False)
        try:
            self._check_descriptor(lock_fd, lock_path)
            lock_exclusive(lock_fd)
            try:
                self._check_descriptor(lock_fd, lock_path)
                try:
                    final.lstat()
                except FileNotFoundError:
                    self.directory = Path(tempfile.mkdtemp(prefix=f".{final.name}.staging-", dir=final.parent))
                    self.database = self.directory / "reservations.sqlite3"
                    if os.name == "nt":
                        make_private_dir(self.directory)
                    self._check_node(self.directory, directory=True)
                    if not is_private_strict(self.directory):
                        raise PermissionError("unsafe reservation directory")
                    self._initialize_database(capacity)
                    with self._connection():
                        pass
                    fd = open_fd(self.database, os.O_RDWR, follow_symlinks=False)
                    try:
                        self._check_descriptor(fd, self.database)
                        os.fsync(fd)
                    finally:
                        os.close(fd)
                    _sync_directory(self.directory)
                    _publish_directory(self.directory, final)
                    _sync_directory(final.parent)
                self.directory = final
                self.database = final / "reservations.sqlite3"
                with self._connection():
                    pass
            finally:
                unlock(lock_fd)
        finally:
            os.close(lock_fd)

    def _initialize_database(self, capacity: int) -> None:
        from ciao.os_support.private import open_private
        fd = open_private(self.database, os.O_RDWR | os.O_EXCL, follow_symlinks=False)
        try:
            self._check_descriptor(fd, self.database)
        finally:
            os.close(fd)
        with self._connection(initialize=True) as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("CREATE TABLE metadata (version INTEGER NOT NULL, capacity INTEGER NOT NULL, high_water REAL)")
            conn.execute("INSERT INTO metadata VALUES (5, ?, NULL)", (capacity,))
            conn.execute("""CREATE TABLE reservations (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                token BLOB NOT NULL UNIQUE, scope BLOB NOT NULL, guard BLOB NOT NULL, owner BLOB,
                state TEXT NOT NULL CHECK(state IN ('reserved','active','finished','sealed')),
                proof_expires REAL)""")
            conn.execute("PRAGMA user_version = 5")
            conn.commit()

    @staticmethod
    def _check_node(path: Path, *, directory: bool = False) -> None:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
                or (not directory and info.st_nlink != 1)):
            raise PermissionError("unsafe reservation storage")

    @classmethod
    def _check_descriptor(cls, fd: int, path: Path) -> None:
        from ciao.os_support.private import is_private_descriptor_strict
        cls._check_node(path)
        opened, named = os.fstat(fd), path.lstat()
        if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or opened.st_nlink != 1 or not is_private_descriptor_strict(fd)):
            raise PermissionError("unsafe reservation file")

    @contextmanager
    def _connection(self, *, initialize: bool = False) -> Iterator[sqlite3.Connection]:
        from ciao.os_support.files import open_fd
        from ciao.os_support.private import (
            is_owned_descriptor,
            is_private_descriptor,
            is_private_descriptor_strict,
            is_private_strict,
        )
        self._check_node(self.directory, directory=True)
        if not is_private_strict(self.directory):
            raise PermissionError("unsafe reservation directory")
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(self.database) + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            try:
                self._check_node(sidecar)
                sidecar_fd = open_fd(
                    sidecar, os.O_RDONLY, follow_symlinks=False, share_delete=os.name == "nt",
                )
            except FileNotFoundError:
                # SQLite can unlink a rollback journal as another connection
                # finishes committing between the existence check and open.
                continue
            try:
                opened = os.fstat(sidecar_fd)
                try:
                    named = sidecar.lstat()
                except FileNotFoundError:
                    continue
                if ((opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                        or opened.st_nlink != 1):
                    raise PermissionError("unsafe reservation sidecar")
                if os.name == "nt":
                    # SQLite creates rollback journals with an inherited DACL.
                    # The protected, owner-only parent bounds those inherited
                    # grants; the file itself must still be owned by this
                    # account and grant access only to the account/SYSTEM.
                    private = is_owned_descriptor(sidecar_fd) and is_private_descriptor(sidecar_fd)
                else:
                    private = is_private_descriptor_strict(sidecar_fd)
                if not private:
                    raise PermissionError("unsafe reservation sidecar")
            finally:
                os.close(sidecar_fd)
        fd = open_fd(self.database, os.O_RDONLY, follow_symlinks=False)
        try:
            self._check_descriptor(fd, self.database)
            conn = sqlite3.connect(self.database.absolute().as_uri() + "?mode=rw", uri=True,
                                   timeout=5, isolation_level=None)
            try:
                self._check_descriptor(fd, self.database)
                conn.execute("PRAGMA synchronous=FULL")
                if conn.execute("PRAGMA journal_mode").fetchone() != ("delete",):
                    raise ValueError("unsupported reservation journal")
                if not initialize:
                    if conn.execute("PRAGMA user_version").fetchone() != (5,):
                        raise ValueError("invalid reservation database version")
                    if conn.execute("SELECT version, capacity FROM metadata").fetchall() != [(5, self.capacity)]:
                        raise ValueError("invalid reservation database metadata")
                    if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
                        raise ValueError("corrupt reservation database")
                yield conn
            finally:
                conn.close()
        finally:
            os.close(fd)

    @staticmethod
    def _hash(domain: bytes, *parts: bytes) -> bytes:
        digest = hashlib.sha256(domain)
        for part in parts:
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
        return digest.digest()

    def _keys(self, token: str, scope: ReservationScope) -> tuple[bytes, bytes]:
        raw = token.encode("ascii")
        public = asdict(scope)
        public.pop("sweep_snapshot")
        canonical = json.dumps(public, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return self._hash(b"ciao-reservation-token-v1", raw), self._hash(b"ciao-reservation-scope-v1", raw, canonical)

    def _guard_key(self, token: str, scope: ReservationScope) -> bytes:
        if scope.kind == "sweep" and tuple(item[0] for item in scope.sweep_snapshot) != scope.workspaces:
            raise ValueError("sweep snapshot unavailable")
        canonical = json.dumps(asdict(scope), sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
        return self._hash(b"ciao-reservation-guard-v1", token.encode("ascii"), canonical)

    def _owner_key(self, token: str, owner: bytes) -> bytes:
        return self._hash(b"ciao-reservation-owner-v1", token.encode("ascii"), owner)

    def _proof_clock(self, conn: sqlite3.Connection) -> tuple[float, bool]:
        now = float(self.clock())
        if not math.isfinite(now):
            raise ValueError("reservation clock unavailable")
        row = conn.execute("SELECT high_water FROM metadata").fetchone()
        if row is None:
            raise ValueError("reservation clock state unavailable")
        high_water = row[0]
        if high_water is not None:
            if not isinstance(high_water, (int, float)) or not math.isfinite(high_water):
                raise ValueError("reservation clock state invalid")
            if now < high_water:
                return float(high_water), False
        conn.execute("UPDATE metadata SET high_water=?", (now,))
        return now, True

    def reserve(self, scope: ReservationScope) -> str:
        token = f"{_EPOCH}.{secrets.token_hex(16)}"
        key, scope_key = self._keys(token, scope)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now, may_expire = self._proof_clock(conn)
            if may_expire:
                conn.execute("DELETE FROM reservations WHERE state='sealed' AND proof_expires<=?", (now,))
            if conn.execute("SELECT COUNT(*) FROM reservations").fetchone()[0] >= self.capacity:
                removed = conn.execute(
                    "DELETE FROM reservations WHERE sequence=(SELECT sequence FROM reservations "
                    "WHERE state='reserved' ORDER BY sequence LIMIT 1)"
                ).rowcount
                if removed != 1:
                    conn.commit()
                    raise OverflowError("reservation capacity exhausted")
            conn.execute("INSERT INTO reservations (token, scope, guard, owner, state) "
                         "VALUES (?, ?, ?, NULL, 'reserved')", (key, scope_key, self._guard_key(token, scope)))
            conn.commit()
        return token

    def admit(self, token: str, scope: ReservationScope) -> bool:
        key, scope_key = self._keys(token, scope)
        owner = secrets.token_bytes(32)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._proof_clock(conn)
            changed = conn.execute("UPDATE reservations SET state='active', owner=? "
                                   "WHERE token=? AND scope=? AND guard=? AND state='reserved'",
                                   (self._owner_key(token, owner), key, scope_key, self._guard_key(token, scope))).rowcount
            conn.commit()
        if changed == 1:
            with self._owners_lock:
                self._owners[token] = owner
        return changed == 1

    def finish(self, token: str) -> bool:
        with self._owners_lock:
            owner = self._owners.get(token)
        if owner is None:
            return False
        key = self._hash(b"ciao-reservation-token-v1", token.encode("ascii"))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._proof_clock(conn)
            changed = conn.execute("UPDATE reservations SET state='finished' WHERE token=? AND owner=? AND state='active'",
                                   (key, self._owner_key(token, owner))).rowcount
            conn.commit()
        if changed == 1:
            with self._owners_lock:
                self._owners.pop(token, None)
        return changed == 1

    async def run(self, token: str, scope: ReservationScope, operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """Retain admission and operation through cancellation of the caller."""
        async def admitted_work() -> _T:
            if not await asyncio.to_thread(self.admit, token, scope):
                raise ReservationAdmissionError("reservation cannot be admitted")
            try:
                result = await operation()
            except asyncio.CancelledError:
                # A worker thread may still be running; do not mark it finished.
                raise
            except BaseException:
                if not await asyncio.to_thread(self.finish, token):
                    raise RuntimeError("reservation completion unavailable")
                raise
            if not await asyncio.to_thread(self.finish, token):
                raise RuntimeError("reservation completion unavailable")
            return result

        task = asyncio.create_task(admitted_work())
        self._tasks.add(task)
        def completed(done: asyncio.Task[_T]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()
        task.add_done_callback(completed)
        return await asyncio.shield(task)

    async def run_sweep(self, token: str, cutoff: str,
                        operation: Callable[[ReservationScope], Coroutine[Any, Any, _T]],
                        snapshot: Callable[[], tuple[tuple[str, str, str, int, int], ...]]) -> _T:
        """Reconstruct the server snapshot, then admit its exact durable digest."""
        def is_reserved() -> bool:
            key = self._hash(b"ciao-reservation-token-v1", token.encode("ascii"))
            with self._connection() as conn:
                row: tuple[Any, ...] | None = conn.execute("SELECT state FROM reservations WHERE token=?", (key,)).fetchone()
                return row == ("reserved",)
        if not await asyncio.to_thread(is_reserved):
            raise ReservationAdmissionError("reservation cannot be admitted")
        captured = await asyncio.to_thread(snapshot)
        names = [item[0] for item in captured]
        scope = validate_scope({"kind": "sweep", "workspace": "", "targets": [cutoff],
                                "workspaces": names}, set(names))
        scope = replace(scope, sweep_snapshot=captured)
        return await self.run(token, scope, lambda: operation(scope))

    def seal(self, token: str, scope: ReservationScope, *, inspected: bool = False) -> bool:
        key, scope_key = self._keys(token, scope)
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            now, may_expire = self._proof_clock(conn)
            if may_expire:
                conn.execute("DELETE FROM reservations WHERE state='sealed' AND proof_expires<=?", (now,))
            row = conn.execute("SELECT state FROM reservations WHERE token=? AND scope=?", (key, scope_key)).fetchone()
            if row is None or row[0] not in {"reserved", "finished", "sealed"} or (row[0] == "finished" and not inspected):
                conn.commit()
                return False
            if row[0] != "sealed":
                conn.execute("UPDATE reservations SET state='sealed', owner=NULL, proof_expires=? WHERE token=? AND scope=?",
                             (now + _PROOF_RETENTION_SECONDS, key, scope_key))
            conn.commit()
        return True


class ReservationRegistry:
    def __init__(self, capacity: int = 512, sealed_capacity: int = 512,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if capacity < 1 or sealed_capacity < 1:
            raise ValueError("reservation capacities must be positive")
        self._capacity = capacity
        self._sealed_capacity = sealed_capacity
        self._lock = threading.Lock()
        self._clock = clock
        self._entries: dict[str, tuple[ReservationScope, str]] = {}
        self._reserved_until: dict[str, float] = {}
        self._sealed: OrderedDict[str, ReservationScope] = OrderedDict()
        self._tasks: set[asyncio.Task[Any]] = set()

    def _expire_reserved(self) -> None:
        now = self._clock()
        expired = [token for token, deadline in self._reserved_until.items() if deadline <= now]
        for token in expired:
            del self._reserved_until[token]
            del self._entries[token]

    def reserve(self, scope: ReservationScope) -> str:
        with self._lock:
            self._expire_reserved()
            if len(self._entries) >= self._capacity:
                raise OverflowError("reservation capacity exhausted")
            token = f"{_EPOCH}.{secrets.token_hex(16)}"
            while token in self._entries or token in self._sealed:
                token = f"{_EPOCH}.{secrets.token_hex(16)}"
            self._entries[token] = (scope, "reserved")
            self._reserved_until[token] = self._clock() + _RESERVED_TTL_SECONDS
            return token

    def admit(self, token: str, scope: ReservationScope) -> bool:
        with self._lock:
            self._expire_reserved()
            if self._entries.get(token) != (scope, "reserved"):
                return False
            self._entries[token] = (scope, "active")
            del self._reserved_until[token]
            return True

    def finish(self, token: str) -> bool:
        with self._lock:
            self._expire_reserved()
            entry = self._entries.get(token)
            if entry is None or entry[1] != "active":
                return False
            self._entries[token] = (entry[0], "finished")
            return True

    async def run(self, token: str, scope: ReservationScope, operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        """Keep admitted work alive if its request task is cancelled."""
        if not self.admit(token, scope):
            raise ReservationAdmissionError("reservation cannot be admitted")
        return await self._run_admitted(token, operation)

    async def run_sweep(self, token: str, cutoff: str,
                        operation: Callable[[ReservationScope], Coroutine[Any, Any, _T]],
                        snapshot: Callable[[], tuple[tuple[str, str, str, int, int], ...]]) -> _T:
        """Admit the reserved cutoff before examining the live workspace set."""
        with self._lock:
            self._expire_reserved()
            entry = self._entries.get(token)
            if (entry is None or entry[1] != "reserved" or entry[0].kind != "sweep"
                    or entry[0].targets != (cutoff,)):
                raise ReservationAdmissionError("reservation cannot be admitted")
            scope = entry[0]
        current = await asyncio.to_thread(snapshot)
        with self._lock:
            self._expire_reserved()
            if (self._entries.get(token) != (scope, "reserved")
                    or current != scope.sweep_snapshot):
                raise ReservationAdmissionError("reservation cannot be admitted")
            self._entries[token] = (scope, "active")
            del self._reserved_until[token]
        return await self._run_admitted(token, lambda: operation(scope))

    async def _run_admitted(self, token: str, operation: Callable[[], Coroutine[Any, Any, _T]]) -> _T:
        try:
            task: asyncio.Task[_T] = asyncio.create_task(operation())
        except BaseException:
            # No operation started, but preserve uncertainty rather than
            # declaring completion if task construction failed.
            raise
        self._tasks.add(task)

        def completed(done: asyncio.Task[_T]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                self.finish(token)
                done.exception()  # observe failures when the requester left

        task.add_done_callback(completed)
        return await asyncio.shield(task)

    def seal(self, token: str, scope: ReservationScope, *, inspected: bool = False) -> bool:
        with self._lock:
            self._expire_reserved()
            sealed_scope = self._sealed.get(token)
            if sealed_scope is not None:
                if sealed_scope != scope:
                    return False
                self._sealed.move_to_end(token)
                return True
            entry = self._entries.get(token)
            if entry is None or entry[0] != scope or entry[1] not in {"reserved", "finished"}:
                return False
            if entry[1] == "finished" and not inspected:
                return False
            del self._entries[token]
            if entry[1] == "reserved":
                del self._reserved_until[token]
            self._sealed[token] = scope
            if len(self._sealed) > self._sealed_capacity:
                self._sealed.popitem(last=False)
            return True


def resolve_receipt_workspace(config: Any, receipt_id: str, requested: str) -> str | None:
    """Resolve a receipt to its actual registered workspace without accepting paths."""
    from ciao.memory_receipts import find_receipt, journal_path

    names = list(config.workspace_names())
    if requested and requested not in names:
        return None
    for name in ([requested] if requested else names):
        vault = Path(config.workspace_vault_root(name))
        if find_receipt(journal_path(vault, None), receipt_id) is not None:
            return name
    return None


async def reserve_proposal_operation(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        workspaces = set(request.app.state.config.workspace_names())
        if isinstance(body, dict) and body.get("kind") == "sweep":
            if set(body) != {"kind", "workspace", "targets"}:
                raise ValueError("invalid sweep scope")
            body = {**body, "workspaces": sorted(workspaces)}
        if isinstance(body, dict) and body.get("kind") == "row_accept" and "destination" in body:
            from ciao.web.proposal_service import _rehome_target, _scan_proposal_rows
            if (set(body) != {"kind", "workspace", "targets", "destination"}
                    or not isinstance(body["destination"], str)
                    or not isinstance(body["targets"], list) or len(body["targets"]) != 1
                    or not isinstance(body["targets"][0], str)):
                raise ValueError("invalid re-home reservation")
            _rows, by_id = await asyncio.to_thread(_scan_proposal_rows, request.app.state.config)
            ctx = by_id.get(body["targets"][0])
            if (ctx is None or ctx.get("row", {}).get("kind") != "rehome"
                    or ctx.get("workspace") != body["workspace"]
                    or ctx.get("row", {}).get("workspace") != body["workspace"]):
                return JSONResponse({"error": "re-home proposal unavailable"}, status_code=404)
            destination, error = _rehome_target(ctx["row"], body["destination"])
            if error or destination not in workspaces or destination == ctx["workspace"]:
                return JSONResponse({"error": error or "invalid re-home destination"}, status_code=400)
            body = {**body, "destination": destination}
        scope = validate_scope(body, workspaces)
        if scope.kind == "sweep":
            snapshot = await asyncio.to_thread(capture_sweep_snapshot, request.app.state.config)
            if tuple(item[0] for item in snapshot) != scope.workspaces:
                raise ValueError("sweep workspace changed")
            scope = replace(scope, sweep_snapshot=snapshot)
        if scope.kind == "batch_accept":
            from ciao.web.proposal_service import _resolve_batch
            resolved, batch_error = await asyncio.to_thread(_resolve_batch, request.app.state.config, list(scope.targets))
            if batch_error or resolved is None:
                return JSONResponse({"error": "batch proposal unavailable"}, status_code=404)
            if sorted({ctx["workspace"] for ctx in resolved}) != list(scope.workspaces):
                raise ValueError("batch workspace mismatch")
            destinations = batch_destinations(resolved, scope.workspace, workspaces)
            scope = validate_scope({**body, "destinations": destinations}, workspaces)
        if scope.kind == "implement":
            from ciao.web.proposal_service import _scan_proposal_rows
            _rows, by_id = await asyncio.to_thread(_scan_proposal_rows, request.app.state.config)
            ctx = by_id.get(scope.targets[0])
            if (ctx is None or not ctx.get("file") or ctx.get("row", {}).get("kind") != "skill"
                    or ctx.get("workspace") != scope.workspace
                    or ctx.get("row", {}).get("workspace") != scope.workspace):
                return JSONResponse({"error": "skill proposal unavailable"}, status_code=404)
        if scope.kind == "undo":
            actual = await asyncio.to_thread(resolve_receipt_workspace, request.app.state.config,
                                             scope.targets[0], scope.workspace)
            if actual is None:
                return JSONResponse({"error": "receipt unavailable"}, status_code=404)
            scope = ReservationScope(scope.kind, actual, scope.targets)
    except (ValueError, TypeError, OSError, KeyError, RuntimeError):
        return JSONResponse({"error": "invalid reservation scope"}, status_code=400)
    try:
        token = await asyncio.to_thread(request.app.state.proposal_reservations.reserve, scope)
    except OverflowError:
        return JSONResponse({"error": "reservation capacity exhausted"}, status_code=503)
    except (OSError, sqlite3.DatabaseError, ValueError):
        return JSONResponse({"error": "reservation storage unavailable"}, status_code=503)
    if scope.kind in _BATCH:
        result_scope: dict[str, Any] = {"kind": scope.kind, "workspace": scope.workspace,
                                        "targets": list(scope.targets), "workspaces": list(scope.workspaces)}
        if scope.kind == "batch_accept":
            result_scope["destinations"] = dict(scope.destinations)
        return JSONResponse({"id": token, "scope": result_scope})
    if scope.kind in {"sweep", "undo"}:
        if scope.kind == "undo":
            return JSONResponse({"id": token, "scope": {"kind": scope.kind, "workspace": scope.workspace,
                                                         "targets": list(scope.targets)}})
        return JSONResponse({"id": token, "scope": {"kind": scope.kind, "workspace": scope.workspace,
                                                       "targets": list(scope.targets), "workspaces": list(scope.workspaces)}})
    if scope.destination is not None:
        return JSONResponse({"id": token, "scope": {"kind": scope.kind, "workspace": scope.workspace,
                                                       "targets": list(scope.targets), "destination": scope.destination}})
    return JSONResponse({"id": token})


async def seal_proposal_operation(request: Request) -> JSONResponse:
    try:
        body = await request.json()
        if not isinstance(body, dict) or set(body) != {"id", "scope", "inspected"} or not isinstance(body["inspected"], bool):
            raise ValueError("invalid request")
        token = body["id"]
        if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}\.[0-9a-f]{32}", token):
            raise ValueError("invalid id")
        # The original workspace may have been removed since the operation.
        # The registry still verifies the complete original scope exactly.
        scope = validate_scope(body["scope"], None)
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid reservation"}, status_code=400)
    try:
        sealed = await asyncio.to_thread(request.app.state.proposal_reservations.seal, token, scope, inspected=body["inspected"])
    except (OSError, sqlite3.DatabaseError, ValueError):
        return JSONResponse({"error": "reservation storage unavailable"}, status_code=503)
    if not sealed:
        return JSONResponse({"error": "reservation unavailable, active, or scope mismatch"}, status_code=409)
    return JSONResponse({"sealed": True})
