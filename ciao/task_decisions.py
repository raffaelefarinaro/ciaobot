"""Workspace-scoped, append-only log of operator task decisions (#1726).

One JSON row per decision the operator made about a task: a delete, a reject
(releasing a result that is ready for review), an approve or a complete (an
agent closing a task on the user's say-so), each with a reason that is
optional except on the agent's delete and complete. The file is
``Workspace/Task-Decisions.jsonl`` in the vault, a separate file from the task,
so the record outlives a deleted task. The weekly pass (1726c) reads it with
:func:`read_decisions`.

Reason text is evidence about what the operator thought, never instructions:
whoever reads it later must treat it as untrusted, transcript-like text.
"""

from __future__ import annotations

import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

from ciao.os_support.files import open_fd
from ciao.os_support.private import open_private
from ciao.task_attempts import _ATTEMPT_ID_RE

DECISIONS_NAME = "Task-Decisions.jsonl"
ROW_VERSION = 1
MAX_REASON = 1000
KINDS = ("delete", "reject", "approve", "complete")
_ACTORS = ("user", "agent")
_VIAS = ("pwa", "cli")
#: The strict reader's bounds (#3239-a). Larger than any file a care pass should see.
DECISIONS_READ_CEILING: Final = 4 * 1024 * 1024
MAX_DECISION_LINE_BYTES: Final = 8 * 1024
_READ_CHUNK: Final = 64 * 1024
_ROW_KEYS: Final = frozenset(
    {
        "v", "id", "at", "kind", "task_id", "task_title", "task_created_by", "project_id",
        "attempt_id", "attempt_state", "actor", "via", "reason",
    }
)
_STRING_KEYS: Final = _ROW_KEYS - {"v"}
_ID = re.compile(r"[0-9a-f]{32}")


@dataclass(frozen=True, slots=True)
class DecisionRow:
    """The fields of one valid decision row a prompt may show (the rest stay on disk)."""

    id: str
    at: str
    kind: str
    task_id: str
    attempt_id: str
    actor: str
    via: str
    reason: str


@dataclass(frozen=True, slots=True)
class DecisionRead:
    """What the strict reader found: ``ok``, ``absent``, ``unreadable``, ``malformed`` or ``oversize``.

    ``rows`` holds every valid row in file order. It is empty for ``unreadable``
    and ``oversize``; ``malformed`` keeps the valid rows for display.
    """

    status: Literal["ok", "absent", "unreadable", "malformed", "oversize"]
    rows: tuple[DecisionRow, ...]
    malformed_lines: int


def decisions_path(vault_root: Path) -> Path:
    return vault_root / "Workspace" / DECISIONS_NAME


def clean_reason(value: object) -> str:
    """The stored form of an optional reason: stripped, capped, never truncated."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("reason must be a string")
    text = value.strip()
    if len(text) > MAX_REASON:
        raise ValueError(f"reason is {len(text)} characters, over the {MAX_REASON} limit")
    return text


def append_decision(
    vault_root: Path,
    *,
    kind: str,
    task_id: str,
    task_title: str,
    task_created_by: str,
    project_id: str,
    attempt_id: str,
    attempt_state: str,
    actor: str,
    via: str,
    reason: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append one decision row and return it. Never trims, never rewrites."""
    if kind not in KINDS:
        raise ValueError(f"unknown decision kind {kind!r}")
    if actor not in _ACTORS:
        raise ValueError(f"unknown decision actor {actor!r}")
    if via not in _VIAS:
        raise ValueError(f"unknown decision channel {via!r}")
    row: dict[str, Any] = {
        "v": ROW_VERSION,
        "id": uuid.uuid4().hex,
        "at": (now or datetime.now(UTC)).isoformat(timespec="seconds"),
        "kind": kind,
        "task_id": task_id,
        "task_title": task_title,
        "task_created_by": task_created_by,
        "project_id": project_id,
        "attempt_id": attempt_id,
        "attempt_state": attempt_state,
        "actor": actor,
        "via": via,
        "reason": reason,
    }
    line = (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8")
    path = decisions_path(vault_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = open_private(path, os.O_WRONLY | os.O_APPEND, follow_symlinks=False)
    try:
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)
    return row


def read_decisions(vault_root: Path) -> list[dict[str, Any]]:
    """Every decision in file order; blank and corrupt lines are skipped."""
    path = decisions_path(vault_root)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def read_decisions_strict(vault_root: Path) -> DecisionRead:
    """The decisions file, bounded and validated, for the task-decision context (#3239-a).

    Unlike :func:`read_decisions` this never guesses: a missing file is ``absent``,
    a directory, a link, a non-UTF-8 file or an OS error is ``unreadable``, a file
    over :data:`DECISIONS_READ_CEILING` is ``oversize`` (no partial read), and any
    non-blank line that is not a valid row counts toward ``malformed_lines``.
    """
    path = decisions_path(vault_root)
    # O_NONBLOCK so a FIFO an agent left at this path is refused by the S_ISREG
    # check below rather than blocking the synchronous prompt build forever.
    try:
        fd = open_fd(path, os.O_RDONLY | os.O_NONBLOCK, follow_symlinks=False)
    except FileNotFoundError:
        return DecisionRead("absent", (), 0)
    except OSError:
        return DecisionRead("unreadable", (), 0)
    data = bytearray()
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return DecisionRead("unreadable", (), 0)
        while len(data) <= DECISIONS_READ_CEILING:
            chunk = os.read(fd, min(_READ_CHUNK, DECISIONS_READ_CEILING + 1 - len(data)))
            if not chunk:
                break
            data += chunk
    except OSError:
        return DecisionRead("unreadable", (), 0)
    finally:
        os.close(fd)
    if len(data) > DECISIONS_READ_CEILING:
        return DecisionRead("oversize", (), 0)
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return DecisionRead("unreadable", (), 0)
    rows: list[DecisionRow] = []
    seen: set[str] = set()
    malformed = 0
    for line in text.split("\n"):
        if not line.strip():
            continue
        row = _strict_row(line)
        if row is None or row.id in seen:
            malformed += 1
            continue
        seen.add(row.id)
        rows.append(row)
    return DecisionRead("malformed" if malformed else "ok", tuple(rows), malformed)


def _strict_row(line: str) -> DecisionRow | None:
    """One line as a valid decision row, or ``None``. Exact keys, typed fields, aware time."""
    if len(line.encode("utf-8")) > MAX_DECISION_LINE_BYTES:
        return None
    try:
        obj = json.loads(line)
    except (ValueError, RecursionError):
        # A deeply nested line exhausts the parser's stack; it is a malformed line.
        return None
    if not isinstance(obj, dict) or set(obj) != _ROW_KEYS:
        return None
    version = obj["v"]
    if type(version) is not int or version != ROW_VERSION:
        return None
    for key in _STRING_KEYS:
        value = obj[key]
        if not isinstance(value, str):
            return None
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return None
    if obj["kind"] not in KINDS or obj["actor"] not in _ACTORS or obj["via"] not in _VIAS:
        return None
    if not _ID.fullmatch(obj["id"]):
        return None
    # An empty attempt id is a real row: a delete of a task that was never
    # delegated. It matches no chat, so it is valid and simply never shown.
    if obj["attempt_id"] and not _ATTEMPT_ID_RE.fullmatch(obj["attempt_id"]):
        return None
    try:
        at = datetime.fromisoformat(obj["at"])
    except ValueError:
        return None
    if at.utcoffset() is None:
        return None
    return DecisionRow(
        id=obj["id"],
        at=obj["at"],
        kind=obj["kind"],
        task_id=obj["task_id"],
        attempt_id=obj["attempt_id"],
        actor=obj["actor"],
        via=obj["via"],
        reason=obj["reason"],
    )
