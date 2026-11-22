"""Workspace-scoped, append-only log of archive captures (#3239-c1).

One JSON line per archive that enqueued a memory pass, in
``Workspace/Archive-Captures.jsonl`` in the vault. The record holds ids and
counts only: the chat and workspace names, the attempt and decision ids the
archive could see, and a status for each read. It never holds transcript text,
decision reasons, task titles, paths or tokens, so there is no free text to
redact.

``captured_at`` comes from the engine clock at the archive call. It is the
trusted first-seen instant a later consumer needs, which a model-written date
cannot be.

Identity is ``(workspace, chat_id, kind, completion_id)``, hashed into the row
id. Rows with one identity fold to the FIRST row, so the original capture time
wins and a resumed chat archived again is not a new sighting. Only a difference
in the other content fields (counts, ids, statuses) makes the identity
unavailable: it is reported in :attr:`CaptureRead.conflicts` and left out of
:attr:`CaptureRead.records`, never resolved silently.

When the live file would pass :data:`CAPTURES_ROTATE_BYTES`, it rolls to a dated
``Archive-Captures.<UTC-date>.jsonl`` sibling and only the newest
:data:`CAPTURES_KEEP_ROTATED` rotated files are kept. The reader reads the live
file only, so a row that rolled out no longer counts for the fold.
"""

from __future__ import annotations

import dataclasses
import errno
import hashlib
import json
import os
import re
import stat
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal

from ciao.os_support.files import open_fd
from ciao.os_support.private import open_private
from ciao.task_attempts import TaskAttempt
from ciao.task_decisions import DecisionRead

CAPTURES_NAME = "Archive-Captures.jsonl"
ROW_VERSION = 1
KINDS: Final = ("archive", "completion")
ATTEMPTS_STATUSES: Final = ("ok", "unreadable")
DECISIONS_STATUSES: Final = ("ok", "absent", "unreadable", "malformed", "oversize")
MAX_ATTEMPTS: Final = 8
MAX_DECISIONS: Final = 20
MAX_TURN_COUNT: Final = 1_000_000
MAX_NAME: Final = 128
MAX_CAPTURE_LINE_BYTES: Final = 4 * 1024
#: The strict reader's bound. Larger than the decisions file's, because captures
#: accumulate one row per archived chat.
CAPTURES_READ_CEILING: Final = 8 * 1024 * 1024
#: The live file rolls to a dated sibling before an append would take it past this,
#: so it stays well under the read ceiling and keeps reading.
CAPTURES_ROTATE_BYTES: Final = CAPTURES_READ_CEILING // 2
#: Rotated files kept beside the live file. The oldest beyond this are deleted.
CAPTURES_KEEP_ROTATED: Final = 4
_MAX_ROTATE_CLASHES: Final = 100
_ROTATED_NAME: Final = re.compile(r"Archive-Captures\.(\d{4}-\d{2}-\d{2})(?:\.(\d+))?\.jsonl")
#: Serializes appends and rotation within this process. The engine is one process.
_APPEND_LOCK: Final = threading.Lock()
_READ_CHUNK: Final = 64 * 1024
_ROW_KEYS: Final = frozenset(
    {
        "v", "id", "kind", "workspace", "chat_id", "captured_at", "turn_count",
        "attempt_ids", "attempts_status", "decision_ids", "decisions_status",
        "omitted_decisions", "completion_id", "complete",
    }
)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_UTC_OFFSET_ZERO = timedelta(0)


@dataclass(frozen=True, slots=True)
class CaptureRecord:
    """One archive capture. Validated on construction; every field is what the row holds."""

    id: str
    kind: str
    workspace: str
    chat_id: str
    captured_at: datetime
    turn_count: int
    attempt_ids: tuple[str, ...]
    attempts_status: str
    decision_ids: tuple[str, ...]
    decisions_status: str
    omitted_decisions: int
    completion_id: str
    complete: bool

    def __post_init__(self) -> None:
        if not _is_name(self.workspace) or not _is_name(self.chat_id):
            raise ValueError("workspace and chat_id must be 1 to 128 characters")
        if self.kind not in KINDS:
            raise ValueError(f"unknown capture kind {self.kind!r}")
        if self.id != capture_id(self.workspace, self.chat_id, self.kind, self.completion_id):
            raise ValueError("capture id does not match its identity")
        if self.kind == "completion":
            if not _HEX32.fullmatch(self.completion_id):
                raise ValueError("a completion capture needs a 32-hex completion id")
        elif self.completion_id:
            raise ValueError("an archive capture carries no completion id")
        if not isinstance(self.captured_at, datetime) or self.captured_at.utcoffset() != _UTC_OFFSET_ZERO:
            raise ValueError("captured_at must be an aware UTC instant")
        if type(self.turn_count) is not int or not 0 <= self.turn_count <= MAX_TURN_COUNT:
            raise ValueError("turn_count is out of range")
        _check_ids(self.attempt_ids, MAX_ATTEMPTS, "attempt_ids")
        _check_ids(self.decision_ids, MAX_DECISIONS, "decision_ids")
        if self.attempts_status not in ATTEMPTS_STATUSES:
            raise ValueError(f"unknown attempts status {self.attempts_status!r}")
        if self.attempts_status == "unreadable" and self.attempt_ids:
            raise ValueError("an unreadable attempt store has no attempt ids")
        if self.decisions_status not in DECISIONS_STATUSES:
            raise ValueError(f"unknown decisions status {self.decisions_status!r}")
        if type(self.omitted_decisions) is not int or self.omitted_decisions < 0:
            raise ValueError("omitted_decisions must be a non-negative int")
        if type(self.complete) is not bool:
            raise ValueError("complete must be a bool")
        if self.complete and not (
            self.attempts_status == "ok"
            and self.decisions_status in ("ok", "absent")
            and self.omitted_decisions == 0
        ):
            raise ValueError("complete is set on a degraded capture")

    def to_row(self) -> dict[str, Any]:
        """The JSON object written for this record, with the version key."""
        return {
            "v": ROW_VERSION,
            "id": self.id,
            "kind": self.kind,
            "workspace": self.workspace,
            "chat_id": self.chat_id,
            "captured_at": self.captured_at.isoformat(timespec="seconds"),
            "turn_count": self.turn_count,
            "attempt_ids": list(self.attempt_ids),
            "attempts_status": self.attempts_status,
            "decision_ids": list(self.decision_ids),
            "decisions_status": self.decisions_status,
            "omitted_decisions": self.omitted_decisions,
            "completion_id": self.completion_id,
            "complete": self.complete,
        }


@dataclass(frozen=True, slots=True)
class CaptureRead:
    """What the strict reader found.

    ``status`` is ``ok``, ``absent``, ``unreadable``, ``malformed`` or ``oversize``.
    ``records`` holds each identity once, in first-appearance order, and omits any
    identity listed in ``conflicts``. A same-identity row that differs from the
    first only in ``captured_at`` is a re-archive and folds silently. ``malformed`` keeps the valid records.
    """

    status: Literal["ok", "absent", "unreadable", "malformed", "oversize"]
    records: tuple[CaptureRecord, ...]
    malformed_lines: int
    conflicts: tuple[str, ...]


def captures_path(vault_root: Path) -> Path:
    return vault_root / "Workspace" / CAPTURES_NAME


def capture_id(workspace: str, chat_id: str, kind: str, completion_id: str) -> str:
    """The deterministic 32-hex id of one capture identity."""
    key = f"{workspace}\0{chat_id}\0{kind}\0{completion_id}".encode()
    return hashlib.sha256(key).hexdigest()[:32]


def build_record(
    *,
    workspace: str,
    chat_id: str,
    kind: str,
    completion_id: str,
    turn_count: int,
    attempts: tuple[TaskAttempt, ...] | None,
    decisions: DecisionRead,
    now: datetime,
) -> CaptureRecord:
    """Build one capture from what the archive saw. Pure: no I/O.

    ``attempts`` is the chat's attempts newest first, as
    ``TaskAttemptStore.attempts_for_chat`` returns them, or ``None`` when that store
    could not be read. ``decisions`` is the strict read of the decisions file.
    Raises ``ValueError`` on a naive ``now`` or any field outside its contract.
    """
    if now.utcoffset() is None:
        raise ValueError("capture time must be timezone-aware")
    captured_at = now.astimezone(UTC)
    if attempts is None:
        attempts_status = "unreadable"
        kept_attempts: tuple[str, ...] = ()
        attempts_overflow = False
    else:
        attempts_status = "ok"
        kept_attempts = tuple(attempt.attempt_id for attempt in attempts[:MAX_ATTEMPTS])
        attempts_overflow = len(attempts) > MAX_ATTEMPTS
    kept_set = set(kept_attempts)
    # The decisions file is append-only, so file order is time order. Newest first
    # is reversed file order, and only rows for a kept attempt are candidates.
    matching = [row.id for row in reversed(decisions.rows) if row.attempt_id in kept_set]
    decision_ids = tuple(matching[:MAX_DECISIONS])
    omitted = len(matching) - len(decision_ids)
    complete = (
        attempts_status == "ok"
        and not attempts_overflow
        and decisions.status in ("ok", "absent")
        and omitted == 0
    )
    return CaptureRecord(
        id=capture_id(workspace, chat_id, kind, completion_id),
        kind=kind,
        workspace=workspace,
        chat_id=chat_id,
        captured_at=captured_at,
        turn_count=turn_count,
        attempt_ids=kept_attempts,
        attempts_status=attempts_status,
        decision_ids=decision_ids,
        decisions_status=decisions.status,
        omitted_decisions=omitted,
        completion_id=completion_id,
        complete=complete,
    )


def append_capture(vault_root: Path, record: CaptureRecord) -> None:
    """Append one record as one line, fsynced. Never trims or rewrites a row.

    Rolls the live file to a dated sibling first when this line would take it past
    :data:`CAPTURES_ROTATE_BYTES`.
    """
    line = (json.dumps(record.to_row(), ensure_ascii=False) + "\n").encode("utf-8")
    if len(line) > MAX_CAPTURE_LINE_BYTES:
        raise ValueError("capture line is over the 4 KiB bound")
    path = captures_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _APPEND_LOCK:
        fd = _open_live(path, len(line))
        try:
            view = memoryview(line)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)


def _open_regular(path: Path) -> int:
    """The live file open for append. Refuses anything that is not a regular file."""
    # O_NONBLOCK so a FIFO with no reader fails with ENXIO instead of blocking the
    # caller (the archive runs on the engine loop). On a regular file it has no
    # effect on the write.
    fd = open_private(path, os.O_WRONLY | os.O_APPEND | os.O_NONBLOCK, follow_symlinks=False)
    try:
        # A FIFO with a reader passes the open; refuse it before a byte is written.
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "the archive captures path is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def _open_live(path: Path, incoming: int) -> int:
    """The live file open for append, rolled first if ``incoming`` bytes would overflow it."""
    fd = _open_regular(path)
    size = os.fstat(fd).st_size
    if size == 0 or size + incoming <= CAPTURES_ROTATE_BYTES:
        return fd
    os.close(fd)
    _rotate(path)
    return _open_regular(path)


def _rotate(path: Path) -> None:
    """Move the live file to the next free dated name, then prune the oldest.

    A hard link refuses an existing target where a rename would replace it, so a
    rotated file is never overwritten. A race with another process may rotate once
    more or leave a duplicate row, which the fold already absorbs.
    """
    day = datetime.now(UTC).strftime("%Y-%m-%d")
    for clash in range(1, _MAX_ROTATE_CLASHES + 1):
        suffix = "" if clash == 1 else f".{clash}"
        target = path.with_name(f"Archive-Captures.{day}{suffix}.jsonl")
        try:
            os.link(path, target, follow_symlinks=False)
        except FileExistsError:
            continue
        except FileNotFoundError:
            return  # another writer already rolled the live file
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        _prune_rotated(path)
        return
    raise OSError(errno.EEXIST, "no free name for a rotated archive captures file")


def _prune_rotated(path: Path) -> None:
    """Delete the oldest rotated files beyond :data:`CAPTURES_KEEP_ROTATED`."""
    rotated: list[tuple[str, int, Path]] = []
    for entry in path.parent.iterdir():
        match = _ROTATED_NAME.fullmatch(entry.name)
        if match is None or entry.is_symlink() or not entry.is_file():
            continue
        rotated.append((match.group(1), int(match.group(2) or 1), entry))
    rotated.sort(key=lambda item: (item[0], item[1]))
    for _day, _clash, entry in rotated[: max(len(rotated) - CAPTURES_KEEP_ROTATED, 0)]:
        try:
            os.unlink(entry)
        except FileNotFoundError:
            pass


def read_captures(vault_root: Path) -> CaptureRead:
    """The captures file, bounded and validated. Never raises on file content.

    A missing file is ``absent``; a directory, a link, a FIFO, a non-UTF-8 file or
    an OS error is ``unreadable``; a file over :data:`CAPTURES_READ_CEILING` is
    ``oversize`` with no partial read. Any non-blank line that is not a valid row
    counts toward ``malformed_lines``. Only the live file is read; rotated files are not.
    """
    path = captures_path(vault_root)
    # O_NONBLOCK so a FIFO left at this path is refused by the S_ISREG check below
    # rather than blocking the caller forever.
    try:
        fd = open_fd(path, os.O_RDONLY | os.O_NONBLOCK, follow_symlinks=False)
    except FileNotFoundError:
        return CaptureRead("absent", (), 0, ())
    except OSError:
        return CaptureRead("unreadable", (), 0, ())
    data = bytearray()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return CaptureRead("unreadable", (), 0, ())
        while len(data) <= CAPTURES_READ_CEILING:
            chunk = os.read(fd, min(_READ_CHUNK, CAPTURES_READ_CEILING + 1 - len(data)))
            if not chunk:
                break
            data += chunk
    except OSError:
        return CaptureRead("unreadable", (), 0, ())
    finally:
        os.close(fd)
    if len(data) > CAPTURES_READ_CEILING:
        return CaptureRead("oversize", (), 0, ())
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return CaptureRead("unreadable", (), 0, ())
    return _fold(text)


def _fold(text: str) -> CaptureRead:
    first: dict[str, CaptureRecord] = {}
    conflicting: list[str] = []
    malformed = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        record = _strict_record(line)
        if record is None:
            malformed += 1
            continue
        seen = first.get(record.id)
        if seen is None:
            first[record.id] = record
        elif (
            dataclasses.replace(record, captured_at=seen.captured_at) != seen
            and record.id not in conflicting
        ):
            conflicting.append(record.id)
    records = tuple(
        record for identity, record in first.items() if identity not in conflicting
    )
    status: Literal["ok", "malformed"] = "malformed" if malformed else "ok"
    return CaptureRead(status, records, malformed, tuple(conflicting))


def _strict_record(line: str) -> CaptureRecord | None:
    """One line as a valid record, or ``None``. Exact keys, typed fields, UTC time."""
    if len(line.encode("utf-8")) > MAX_CAPTURE_LINE_BYTES:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict) or set(obj) != _ROW_KEYS:
        return None
    if type(obj["v"]) is not int or obj["v"] != ROW_VERSION:
        return None
    attempt_ids = obj["attempt_ids"]
    decision_ids = obj["decision_ids"]
    if not isinstance(attempt_ids, list) or not isinstance(decision_ids, list):
        return None
    try:
        captured_at = datetime.fromisoformat(obj["captured_at"])
        return CaptureRecord(
            id=obj["id"],
            kind=obj["kind"],
            workspace=obj["workspace"],
            chat_id=obj["chat_id"],
            captured_at=captured_at,
            turn_count=obj["turn_count"],
            attempt_ids=tuple(attempt_ids),
            attempts_status=obj["attempts_status"],
            decision_ids=tuple(decision_ids),
            decisions_status=obj["decisions_status"],
            omitted_decisions=obj["omitted_decisions"],
            completion_id=obj["completion_id"],
            complete=obj["complete"],
        )
    except (ValueError, TypeError):
        return None


def _is_name(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= MAX_NAME


def _check_ids(values: tuple[str, ...], cap: int, label: str) -> None:
    if len(values) > cap:
        raise ValueError(f"{label} holds more than {cap} ids")
    if len(set(values)) != len(values):
        raise ValueError(f"{label} repeats an id")
    for value in values:
        if not isinstance(value, str) or not _HEX32.fullmatch(value):
            raise ValueError(f"{label} holds a value that is not a 32-hex id")
