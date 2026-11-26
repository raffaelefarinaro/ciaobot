"""Durable, bounded, tamper-evident audit for shared-brain stewardship (#3628).

`DurableAuditLog` is the restricted audit the steward authority core
(`ciao.shared_brain_authority`) writes around every authority transition. It is
storage only: no route, no Settings surface and no background job use it yet.

* Owner-private. The injected root is 0700 and every file in it 0600; each open
  is by descriptor, refuses a symlink and runs the strict owner/mode/platform-ACL
  verifier before the bytes are trusted.
* Hash chained. Each line carries the SHA-256 of the previous line's hash plus
  its own canonical body. Reordering, editing, inserting or removing a record in
  the middle fails verification, and a state file records the last sequence and
  hash so removing the tail fails too. The chain is unkeyed: it detects damage
  and a writer that does not recompute it, not an attacker who can rewrite the
  whole directory as the owner. That boundary is the owner-private root, whose
  placement the integration owner decides.
* Verified on every read. A malformed, unsafe, gapped or inconsistent log is
  refused (`AuditError`); it is never read as an empty audit.
* Crash recovery. A record is appended and fsynced before the state file
  advances. A crash between the two leaves one valid record beyond the state,
  which the next open adopts; a torn final line of the active segment is
  truncated. Anything further out is corruption. A crash during rotation or
  pruning is reconciled by the next open.
* Bounded with rotation, not silent truncation. The active segment holds
  `segment_records` records and is then sealed under a new name. At most
  `max_segments` sealed segments are kept; dropping the oldest advances an
  explicit anchor (first retained sequence, the hash it chains from and a
  running `pruned` count) in the state file, so retention is visible in
  `DurableAuditLog.view` and the retained chain still verifies.

Activation requirements this module does not meet by itself:

* Pruning can drop an unresolved intent (and a resolution can outlive its
  intent). Before any caller enables retention-driven pruning, it must settle
  every pending intent first, or archive sealed segments instead of deleting.
* Rollback is undetected: restoring the state file and the log together to an
  older valid copy verifies. The chain is unkeyed and local.
* An append can raise after its record is durable (the state write or a prune
  failed). The entry exists and the next open adopts it; callers treat such an
  error as "maybe written".
* The recorded storage is whatever the caller supplies; the authority engine
  supplies a digest, never a URL that could carry credentials.

Entries are intents and resolutions. Every entry names its lease owner (pid and
boot id); another process settles an intent only when its owner is gone, or the
intent is older than ``INTENT_MAX_AGE_S``. At most one resolution per intent. The authority engine writes an `intent`
before a mutation and a `committed` or `aborted` entry after, so an interrupted
commit is reconcilable from the authority record without replaying any
admission. No nonce, capability seal or evidence is ever written.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from ciao.os_support.files import open_fd, replace_file
from ciao.os_support.locks import lock_exclusive, unlock
from ciao.os_support.private import (
    is_private_descriptor_strict,
    make_private_dir,
    mkstemp_private,
    open_private,
)

#: Records per segment before it is sealed.
SEGMENT_RECORDS = 256

#: Sealed segments retained before the oldest is dropped behind the anchor.
MAX_SEGMENTS = 16

#: Chain origin: the hash before the first record ever written.
GENESIS_HASH = "0" * 64

#: Longest text field, in UTF-8 bytes, and longest record line. A field over the
#: cap is refused on write and treated as corruption on read, so a hostile or
#: damaged file cannot make a read allocate or parse without bound.
MAX_FIELD_BYTES = 512
MAX_LINE_BYTES = 8192

#: An intent whose owner's liveness cannot be determined (a pid that exists
#: under another or unreadable boot id) is settled once it is this old, well
#: beyond any admission lifetime. A demonstrably live owner is never settled by
#: age, so a hung committer or a wall-clock step cannot make its audit false.
INTENT_MAX_AGE_S = 3600.0

#: State schema. Any other value is corrupt.
SCHEMA_VERSION = 1

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
_ACTIVE = "audit.log"
_STATE = "audit.state.json"
_LOCK = "audit.lock"
_SEALED = re.compile(r"^audit\.(\d{12})\.log$")
_HASH = re.compile(r"^[0-9a-f]{64}$")


class AuditKind(str, Enum):
    """What an entry says about one transition attempt."""

    INTENT = "intent"
    COMMITTED = "committed"
    ABORTED = "aborted"


class AuditError(Exception):
    """The audit could not be read, verified or written.

    `reason` is one of corrupt, unsafe_root, unsafe_store,
    durability_unavailable or invalid_entry.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """One verified audit record."""

    seq: int
    kind: AuditKind
    intent: str
    operation: str
    brain: str
    storage: str
    actor: str
    previous_steward: str | None
    steward: str
    previous_generation: int
    generation: int
    at: float
    recovered: bool
    owner: str
    hash: str


@dataclass(frozen=True, slots=True)
class AuditView:
    """The retained, verified audit. `pruned` counts rotated-out records."""

    entries: tuple[AuditEntry, ...]
    first_seq: int
    pruned: int


@dataclass
class _State:
    last_seq: int = 0
    last_hash: str = GENESIS_HASH
    base_seq: int = 1
    base_prev_hash: str = GENESIS_HASH
    pruned: int = 0


_BODY_KEYS = frozenset(
    {
        "seq",
        "kind",
        "intent",
        "operation",
        "brain",
        "storage",
        "actor",
        "previous_steward",
        "steward",
        "previous_generation",
        "generation",
        "at",
        "recovered",
        "owner",
    }
)


_BOOT_CACHE: list[str] = []


def _boot_id() -> str:
    """An identifier that usually changes on reboot; "unknown" when it cannot be read.

    Only a successful read is cached: a transient failure must not pin
    "unknown" for the life of the process. On macOS the value is the boot time
    from sysctl, which can shift when the clock is stepped, so a mismatch never
    proves a reboot on its own (see :func:`owner_state`). In a Linux container
    this is the host's boot id and a pid is namespace-local, so sharing one
    audit directory between containers (or between a container and its host) is
    unsupported: another namespace's pid would read as dead.
    """
    if _BOOT_CACHE:
        return _BOOT_CACHE[0]
    found = "unknown"
    try:
        with open("/proc/sys/kernel/random/boot_id", encoding="ascii") as handle:
            found = handle.read().strip() or "unknown"
    except OSError:
        try:
            done = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.boottime"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=5,
                check=False,
            )
            text = done.stdout.strip()
            if done.returncode == 0 and text:
                found = text.replace(" ", "")
        except (OSError, subprocess.SubprocessError):
            pass
    if found != "unknown":
        _BOOT_CACHE.append(found)
    return found


def current_owner() -> str:
    """The lease owner of an intent written by this process: pid and boot id."""
    return f"{os.getpid()}:{_boot_id()}"


def owner_state(owner: str) -> str:
    """"self", "alive", "dead" or "unknown" for an intent owner string.

    "self" is this very process (whatever its boot id says): the caller's own
    live-intent registry decides. A pid that does not exist is "dead". A pid
    that exists and carries the same, known boot id is "alive". A pid that
    exists under a different or unreadable boot id is "unknown": it may be a
    recycled pid after a reboot or the same process after a clock step shifted
    the boot time, and only the caller's age rule can tell. A live owner is
    never settled by age alone; age applies to "unknown" only.
    """
    pid_text, _, boot = owner.partition(":")
    if not pid_text.isdigit():
        return "dead"
    pid = int(pid_text)
    if pid == os.getpid():
        return "self"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        pass
    except OSError:
        return "dead"
    here = _boot_id()
    return "alive" if boot == here and here != "unknown" else "unknown"


def _chain(previous: str, body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256((previous + "\n" + canonical).encode("ascii")).hexdigest()


def _is_text(value: object) -> bool:
    return isinstance(value, str) and bool(value)


def _entry_from_body(body: Any, digest: Any, error: str = "corrupt") -> AuditEntry:
    """A validated entry or AuditError(error); fields are checked, not coerced."""
    if not isinstance(body, dict) or set(body) != _BODY_KEYS or not isinstance(digest, str):
        raise AuditError(error)
    if not _HASH.match(digest):
        raise AuditError(error)
    try:
        kind = AuditKind(body["kind"])
    except ValueError:
        raise AuditError(error) from None
    previous = body["previous_steward"]
    if (
        type(body["seq"]) is not int
        or body["seq"] < 1
        or not all(
            _is_text(body[k])
            for k in ("intent", "operation", "brain", "storage", "actor", "steward", "owner")
        )
        or not (previous is None or _is_text(previous))
        or type(body["previous_generation"]) is not int
        or type(body["generation"]) is not int
        or body["previous_generation"] < 0
        or body["generation"] < 1
        or type(body["at"]) not in (int, float)
        or not math.isfinite(body["at"])
        or type(body["recovered"]) is not bool
    ):
        raise AuditError(error)
    try:
        for key in ("intent", "operation", "brain", "storage", "actor", "steward", "owner"):
            if len(body[key].encode("utf-8")) > MAX_FIELD_BYTES:
                raise AuditError(error)
        if previous is not None and len(previous.encode("utf-8")) > MAX_FIELD_BYTES:
            raise AuditError(error)
    except UnicodeEncodeError:
        raise AuditError(error) from None
    return AuditEntry(
        seq=body["seq"],
        kind=kind,
        intent=body["intent"],
        operation=body["operation"],
        brain=body["brain"],
        storage=body["storage"],
        actor=body["actor"],
        previous_steward=previous,
        steward=body["steward"],
        previous_generation=body["previous_generation"],
        generation=body["generation"],
        at=float(body["at"]),
        recovered=body["recovered"],
        owner=body["owner"],
        hash=digest,
    )


class DurableAuditLog:
    """A private, hash-chained, rotating audit log in an injected directory."""

    def __init__(
        self,
        root: str | os.PathLike[str],
        *,
        segment_records: int = SEGMENT_RECORDS,
        max_segments: int = MAX_SEGMENTS,
    ) -> None:
        if segment_records < 1 or max_segments < 1:
            raise ValueError("an audit log needs positive segment bounds")
        self._segment_records = segment_records
        self._max_segments = max_segments
        self._root = Path(root)
        if self._root.is_symlink():
            raise AuditError("unsafe_root")
        try:
            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            make_private_dir(self._root)
        except OSError:
            raise AuditError("unsafe_root") from None
        if not self._root.is_dir() or self._root.is_symlink():
            raise AuditError("unsafe_root")
        self._thread_lock = threading.Lock()
        self._require_private_root()

    # ── Public ───────────────────────────────────────────────────────────

    def append(
        self,
        kind: AuditKind,
        *,
        intent: str,
        operation: str,
        brain: str,
        storage: str,
        actor: str,
        previous_steward: str | None,
        steward: str,
        previous_generation: int,
        generation: int,
        at: float,
        recovered: bool = False,
        owner: str | None = None,
    ) -> AuditEntry:
        """Verify, rotate if full, append one record durably, advance the state.

        An intent id is used once; a resolution needs a retained unresolved
        intent with the same operation, brain, storage, steward and generation,
        and a second resolution for one intent is refused
        (``duplicate_resolution``), which is what keeps two engines from both
        settling one intent. ``at`` must be finite and every text field at most
        ``MAX_FIELD_BYTES``. A failure after the record is durable (the state
        write or a prune) can still raise: the entry then exists and the next
        open adopts it, so a caller must treat such an error as "maybe written".
        """
        with self._locked():
            state, entries = self._load_locked()
            self._check_resolution(kind, intent, entries, operation, brain, storage, steward, generation)
            body: dict[str, Any] = {
                "seq": state.last_seq + 1,
                "kind": AuditKind(kind).value,
                "intent": intent,
                "operation": operation,
                "brain": brain,
                "storage": storage,
                "actor": actor,
                "previous_steward": previous_steward,
                "steward": steward,
                "previous_generation": previous_generation,
                "generation": generation,
                "at": at,
                "recovered": recovered,
                "owner": owner if owner is not None else current_owner(),
            }
            digest = _chain(state.last_hash, body)
            entry = _entry_from_body(body, digest, "invalid_entry")
            self._rotate_if_full(entries)
            line = json.dumps({"body": body, "hash": digest}, sort_keys=True) + "\n"
            self._append_line(line)
            state.last_seq = entry.seq
            state.last_hash = digest
            self._write_state(state)
            self._prune(state)
            return entry

    def view(self) -> AuditView:
        """The verified retained entries, oldest first."""
        with self._locked():
            state, entries = self._load_locked()
            return AuditView(
                entries=tuple(entries), first_seq=state.base_seq, pruned=state.pruned
            )

    def pending(self) -> tuple[AuditEntry, ...]:
        """Intents with no committed or aborted resolution, oldest first."""
        entries = self.view().entries
        resolved = {e.intent for e in entries if e.kind is not AuditKind.INTENT}
        return tuple(
            e for e in entries if e.kind is AuditKind.INTENT and e.intent not in resolved
        )

    @staticmethod
    def _check_resolution(
        kind: AuditKind,
        intent: str,
        entries: list[AuditEntry],
        operation: str,
        brain: str,
        storage: str,
        steward: str,
        generation: int,
    ) -> None:
        same = [e for e in entries if e.intent == intent]
        if kind is AuditKind.INTENT:
            if same:
                raise AuditError("invalid_entry")
            return
        opened = [e for e in same if e.kind is AuditKind.INTENT]
        if not opened:
            raise AuditError("invalid_entry")
        if any(e.kind is not AuditKind.INTENT for e in same):
            raise AuditError("duplicate_resolution")
        first = opened[0]
        if (first.operation, first.brain, first.storage, first.steward, first.generation) != (
            operation,
            brain,
            storage,
            steward,
            generation,
        ):
            raise AuditError("invalid_entry")

    # ── Loading and verification ─────────────────────────────────────────

    def _load_locked(self) -> tuple[_State, list[AuditEntry]]:
        self._remove_stale_temporaries()
        state = self._read_state()
        sealed = self._sealed_segments()
        # A prune that crashed after the anchor advanced: drop what it gave up.
        for index in range(len(sealed) - 1):
            if sealed[index + 1][0] <= state.base_seq:
                self._unlink(sealed[index][1])
        sealed = self._sealed_segments()
        files: list[tuple[int | None, Path]] = list(sealed)
        files.append((None, self._root / _ACTIVE))
        entries: list[AuditEntry] = []
        seen: dict[str, AuditEntry | None] = {}
        previous = state.base_prev_hash
        expected_seq = state.base_seq
        for position, (first, path) in enumerate(files):
            is_active = position == len(files) - 1
            raw = self._read_file(path, missing_ok=is_active)
            if raw is None:
                continue
            if len(raw) > MAX_LINE_BYTES * (self._segment_records + 1):
                raise AuditError("corrupt")
            lines, torn = self._split(raw, allow_torn=is_active)
            if first is not None and not lines:
                raise AuditError("corrupt")
            for index, line in enumerate(lines):
                if len(line) > MAX_LINE_BYTES:
                    raise AuditError("corrupt")
                entry, body = self._parse_line(line)
                if entry.seq != expected_seq or (index == 0 and first not in (None, entry.seq)):
                    raise AuditError("corrupt")
                if entry.hash != _chain(previous, body):
                    raise AuditError("corrupt")
                self._check_chain_rules(entry, seen, state)
                previous = entry.hash
                expected_seq += 1
                entries.append(entry)
            if torn:
                self._truncate_active(path, sum(len(line) + 1 for line in lines))
        last_seq = expected_seq - 1
        if last_seq == state.last_seq and previous == state.last_hash:
            return state, entries
        if last_seq == state.last_seq + 1 and entries:
            # Crash after the record was durable and before the state advanced.
            state.last_seq = last_seq
            state.last_hash = previous
            self._write_state(state)
            return state, entries
        raise AuditError("corrupt")

    @staticmethod
    def _check_chain_rules(
        entry: AuditEntry, seen: dict[str, AuditEntry | None], state: _State
    ) -> None:
        """One intent opens once and resolves at most once, in that order."""
        if entry.kind is AuditKind.INTENT:
            if entry.intent in seen:
                raise AuditError("corrupt")
            seen[entry.intent] = None
            return
        if entry.intent not in seen:
            # Its intent may only be missing because retention dropped it.
            if state.pruned == 0:
                raise AuditError("corrupt")
        elif seen[entry.intent] is not None:
            raise AuditError("corrupt")
        seen[entry.intent] = entry

    def _remove_stale_temporaries(self) -> None:
        """Delete state-write temporaries a crash left behind (under the lock)."""
        try:
            names = os.listdir(self._root)
        except OSError:
            raise AuditError("unsafe_root") from None
        for name in names:
            if name.startswith(".audit.") and name.endswith(".tmp"):
                try:
                    (self._root / name).unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    raise AuditError("durability_unavailable") from None

    @staticmethod
    def _split(raw: bytes, *, allow_torn: bool) -> tuple[list[bytes], bool]:
        if not raw:
            return [], False
        parts = raw.split(b"\n")
        tail = parts.pop()
        if tail == b"":
            return parts, False
        if not allow_torn:
            raise AuditError("corrupt")
        return parts, True

    @staticmethod
    def _parse_line(line: bytes) -> tuple[AuditEntry, dict[str, Any]]:
        try:
            document = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise AuditError("corrupt") from None
        if not isinstance(document, dict) or set(document) != {"body", "hash"}:
            raise AuditError("corrupt")
        entry = _entry_from_body(document["body"], document["hash"])
        return entry, document["body"]

    def _sealed_segments(self) -> list[tuple[int, Path]]:
        found: list[tuple[int, Path]] = []
        try:
            names = os.listdir(self._root)
        except OSError:
            raise AuditError("unsafe_root") from None
        for name in names:
            match = _SEALED.match(name)
            if match:
                found.append((int(match.group(1)), self._root / name))
        found.sort()
        return found

    # ── Writing ──────────────────────────────────────────────────────────

    def _rotate_if_full(self, entries: list[AuditEntry]) -> None:
        active = self._root / _ACTIVE
        raw = self._read_file(active, missing_ok=True)
        count = 0 if raw is None else raw.count(b"\n")
        if count < self._segment_records:
            return
        first = entries[-count].seq
        try:
            replace_file(active, self._root / f"audit.{first:012d}.log")
        except OSError:
            raise AuditError("durability_unavailable") from None
        self._fsync_root()

    def _append_line(self, line: str) -> None:
        path = self._root / _ACTIVE
        created = not path.exists()
        try:
            descriptor = open_private(
                path, os.O_WRONLY | os.O_APPEND, 0o600, follow_symlinks=False
            )
        except OSError:
            raise AuditError("unsafe_store") from None
        try:
            try:
                if not is_private_descriptor_strict(descriptor):
                    raise AuditError("unsafe_store")
            except OSError:
                raise AuditError("unsafe_store") from None
            data = line.encode("ascii")
            try:
                written = 0
                while written < len(data):
                    written += os.write(descriptor, data[written:])
                os.fsync(descriptor)
            except OSError:
                raise AuditError("durability_unavailable") from None
        finally:
            os.close(descriptor)
        if created:
            self._fsync_root()

    def _prune(self, state: _State) -> None:
        sealed = self._sealed_segments()
        while len(sealed) > self._max_segments:
            _, path = sealed[0]
            following = sealed[1][0]  # more than max_segments >= 1 sealed, so a successor exists
            raw = self._read_file(path, missing_ok=False)
            lines = (raw or b"").split(b"\n")[:-1]
            if not lines:
                raise AuditError("corrupt")
            dropped_last, _ = self._parse_line(lines[-1])
            state.base_seq = following
            state.base_prev_hash = dropped_last.hash
            state.pruned += len(lines)
            self._write_state(state)  # the anchor moves first; the unlink follows
            self._unlink(path)
            sealed = sealed[1:]

    def _unlink(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            raise AuditError("durability_unavailable") from None
        self._fsync_root()

    def _truncate_active(self, path: Path, keep: int) -> None:
        try:
            descriptor = open_fd(path, os.O_WRONLY, follow_symlinks=False)
        except OSError:
            raise AuditError("unsafe_store") from None
        try:
            os.ftruncate(descriptor, keep)
            os.fsync(descriptor)
        except OSError:
            raise AuditError("durability_unavailable") from None
        finally:
            os.close(descriptor)

    def _read_state(self) -> _State:
        raw = self._read_file(self._root / _STATE, missing_ok=True)
        if raw is None:
            return _State()
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            raise AuditError("corrupt") from None
        keys = {"schema", "last_seq", "last_hash", "base_seq", "base_prev_hash", "pruned"}
        if (
            not isinstance(document, dict)
            or set(document) != keys
            or type(document["schema"]) is not int
            or document["schema"] != SCHEMA_VERSION
            or any(
                type(document[k]) is not int or document[k] < 0 for k in ("last_seq", "pruned")
            )
            or type(document["base_seq"]) is not int
            or document["base_seq"] < 1
            or not all(
                isinstance(document[k], str) and _HASH.match(document[k])
                for k in ("last_hash", "base_prev_hash")
            )
        ):
            raise AuditError("corrupt")
        return _State(
            last_seq=document["last_seq"],
            last_hash=document["last_hash"],
            base_seq=document["base_seq"],
            base_prev_hash=document["base_prev_hash"],
            pruned=document["pruned"],
        )

    def _write_state(self, state: _State) -> None:
        document = {
            "schema": SCHEMA_VERSION,
            "last_seq": state.last_seq,
            "last_hash": state.last_hash,
            "base_seq": state.base_seq,
            "base_prev_hash": state.base_prev_hash,
            "pruned": state.pruned,
        }
        descriptor, temp_name = mkstemp_private(dir=self._root, prefix=".audit.", suffix=".tmp")
        temporary = Path(temp_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
                handle.write(json.dumps(document, sort_keys=True, indent=2) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            replace_file(temporary, self._root / _STATE)
        except OSError:
            raise AuditError("durability_unavailable") from None
        finally:
            temporary.unlink(missing_ok=True)
        self._fsync_root()

    # ── Descriptor-level safety ──────────────────────────────────────────

    def _read_file(self, path: Path, *, missing_ok: bool) -> bytes | None:
        try:
            descriptor = open_fd(path, os.O_RDONLY, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return None
            raise AuditError("corrupt") from None
        except OSError:
            raise AuditError("unsafe_store") from None
        try:
            try:
                if not is_private_descriptor_strict(descriptor):
                    raise AuditError("unsafe_store")
            except OSError:
                raise AuditError("unsafe_store") from None
            chunks: list[bytes] = []
            try:
                while True:
                    chunk = os.read(descriptor, 1 << 16)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except OSError:
                raise AuditError("corrupt") from None
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _private_root_fd(self) -> int:
        try:
            descriptor = open_fd(self._root, _DIRECTORY_FLAGS, follow_symlinks=False)
        except OSError:
            raise AuditError("unsafe_root") from None
        try:
            try:
                private = is_private_descriptor_strict(descriptor)
            except OSError:
                raise AuditError("unsafe_root") from None
            if not private:
                raise AuditError("unsafe_root")
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor

    def _require_private_root(self) -> None:
        os.close(self._private_root_fd())

    def _fsync_root(self) -> None:
        descriptor = self._private_root_fd()
        try:
            try:
                os.fsync(descriptor)
            except OSError:
                raise AuditError("durability_unavailable") from None
        finally:
            os.close(descriptor)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            self._require_private_root()
            try:
                descriptor = open_private(
                    self._root / _LOCK, os.O_RDWR, 0o600, follow_symlinks=False
                )
            except OSError:
                raise AuditError("unsafe_store") from None
            try:
                try:
                    if not is_private_descriptor_strict(descriptor):
                        raise AuditError("unsafe_store")
                except OSError:
                    raise AuditError("unsafe_store") from None
                lock_exclusive(descriptor)
                try:
                    yield
                finally:
                    unlock(descriptor)
            finally:
                os.close(descriptor)
