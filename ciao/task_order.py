"""The authoritative manual order of a workspace's task board (#2086, part of #2079).

Task files stay the source of truth for membership; this module owns only the
*sequence* each status column is shown in. The sequence lives in one JSON
file per workspace, ``<vault>/Workspace/task-order.json``, never in task
frontmatter (a rank there would rewrite task files and invalidate their
revisions). The module is pure and stdlib-only: the disk I/O, the lock and
the atomic write belong to :class:`ciao.task_board.TaskBoardStore`.

Three ideas:

**Baseline.** With no stored order, tasks sort by :func:`baseline_key`: dated
first, then due date, then an accent- and case-insensitive title, then id.
This is the single source of truth for that order; no client sorts tasks.

**Reconcile on read.** :func:`reconcile` merges the stored sequences with the
tasks that actually exist: ids that vanished or changed status are ignored,
and tasks the file does not list (hand-created, or a legacy board) are
appended in baseline order. Reads never write.

**Placement.** :func:`apply_placement` moves one id into a status sequence
next to an anchor the client chose among the tasks it can see; hidden tasks
keep their relative order because the insertion is done on the full sequence.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ciao.task_board import TaskRecord

ORDER_RELATIVE = Path("Workspace") / "task-order.json"
ORDER_SCHEMA = 1
MAX_ORDER_BYTES = 1_048_576

#: The board's statuses, in column order.
STATUSES = ("backlog", "in_progress", "in_review", "done")

_ID_RE = re.compile(r"[0-9a-f]{32}")


class TaskOrderError(Exception):
    """A refused order operation. ``code`` is ``invalid_order`` or ``order_conflict``."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class TaskOrder:
    """One workspace's order: a version and one id sequence per status.

    ``version`` is 0 when no order file exists and -1 when the file is
    unreadable (no client version can match it). All four statuses are
    always present in ``sequences``.
    """

    version: int
    sequences: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class Placement:
    """Where to put a task in its destination status.

    At most one of ``before_id`` / ``after_id``; neither means append.
    ``expected_version`` is the order version the client last saw.
    """

    before_id: str | None
    after_id: str | None
    expected_version: int


def empty_order(version: int = 0) -> TaskOrder:
    return TaskOrder(version=version, sequences={status: () for status in STATUSES})


def title_key(title: str) -> str:
    """Accent-stripped, case-folded title used for the baseline tie-break."""
    decomposed = unicodedata.normalize("NFKD", title)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return stripped.casefold()


def baseline_key(record: TaskRecord) -> tuple[bool, str, str, str]:
    """Sort key of the baseline order: dated first, then title, then id."""
    return (record.due is None, record.due or "", title_key(record.title), record.id)


def parse_order(raw: bytes) -> TaskOrder:
    """Parse the order file strictly; a bad shape raises ``ValueError``."""
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"not valid JSON: {exc}") from None
    if not isinstance(data, dict):
        raise ValueError("top level must be an object")
    if data.get("schema") != ORDER_SCHEMA or isinstance(data.get("schema"), bool):
        raise ValueError(f"unsupported schema {data.get('schema')!r}")
    version = data.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("version must be an integer >= 1")
    sequences = data.get("sequences")
    if not isinstance(sequences, dict) or set(sequences) != set(STATUSES):
        raise ValueError(f"sequences must have exactly the keys {list(STATUSES)}")
    seen: set[str] = set()
    parsed: dict[str, tuple[str, ...]] = {}
    for status in STATUSES:
        ids = sequences[status]
        if not isinstance(ids, list):
            raise ValueError(f"sequence {status!r} must be a list")
        for task_id in ids:
            if not isinstance(task_id, str) or not _ID_RE.fullmatch(task_id):
                raise ValueError(f"sequence {status!r} holds a non-task id: {task_id!r}")
            if task_id in seen:
                raise ValueError(f"id {task_id} is listed more than once")
            seen.add(task_id)
        parsed[status] = tuple(ids)
    return TaskOrder(version=version, sequences=parsed)


def render_order(order: TaskOrder) -> bytes:
    """Deterministic file bytes: indent 2, fixed key order, trailing newline."""
    document: dict[str, Any] = {
        "schema": ORDER_SCHEMA,
        "version": order.version,
        "sequences": {status: list(order.sequences[status]) for status in STATUSES},
    }
    return (json.dumps(document, indent=2) + "\n").encode("utf-8")


def reconcile(stored: TaskOrder | None, records: Sequence[TaskRecord]) -> TaskOrder:
    """The effective order: stored ids that still fit, then the unlisted tasks.

    Orphan ids, wrong-status ids and duplicates in the stored order are
    ignored; unlisted tasks of a status follow in baseline order. The version
    is the stored one (0 without a file). Never writes.
    """
    by_id = {record.id: record for record in records}
    placed: set[str] = set()
    sequences: dict[str, tuple[str, ...]] = {}
    for status in STATUSES:
        kept: list[str] = []
        if stored is not None:
            for task_id in stored.sequences.get(status, ()):
                record = by_id.get(task_id)
                if record is None or record.status != status or task_id in placed:
                    continue
                placed.add(task_id)
                kept.append(task_id)
        unlisted = sorted(
            (r for r in records if r.status == status and r.id not in placed),
            key=baseline_key,
        )
        kept.extend(record.id for record in unlisted)
        placed.update(record.id for record in unlisted)
        sequences[status] = tuple(kept)
    return TaskOrder(version=stored.version if stored is not None else 0, sequences=sequences)


def apply_placement(
    order: TaskOrder,
    task_id: str,
    new_status: str,
    placement: Placement | None,
    *,
    known_ids: Collection[str],
) -> TaskOrder:
    """Move ``task_id`` into ``new_status`` and return the order at ``version + 1``.

    ``order`` is a reconciled order; ``known_ids`` is every task id that
    exists after the operation (the moved id included), and ids outside it
    are pruned. Without a placement the id is appended to the destination
    with no version check. With one, ``expected_version`` must equal
    ``order.version`` (``order_conflict``) and the anchors are validated
    (``invalid_order``): both given, an anchor equal to the moved id, or an
    anchor not in the destination sequence. The insertion happens on the full
    sequence, so tasks the client cannot see keep their relative order.
    """
    if new_status not in STATUSES:
        raise TaskOrderError("invalid_order", f"unknown status {new_status!r}")
    if task_id not in known_ids:
        raise TaskOrderError("invalid_order", f"unknown task {task_id}")
    if placement is not None:
        if placement.expected_version != order.version:
            raise TaskOrderError(
                "order_conflict",
                "the task order changed since it was read; nothing was written",
            )
        if placement.before_id is not None and placement.after_id is not None:
            raise TaskOrderError("invalid_order", "give before_id or after_id, not both")
        for anchor in (placement.before_id, placement.after_id):
            if anchor is not None and anchor == task_id:
                raise TaskOrderError("invalid_order", "a task cannot be placed relative to itself")
    sequences = {
        status: [i for i in order.sequences.get(status, ()) if i != task_id and i in known_ids]
        for status in STATUSES
    }
    destination = sequences[new_status]
    index = len(destination)
    if placement is not None:
        for anchor, offset in ((placement.before_id, 0), (placement.after_id, 1)):
            if anchor is None:
                continue
            if anchor not in destination:
                raise TaskOrderError(
                    "invalid_order", f"anchor {anchor} is not in the {new_status} sequence"
                )
            index = destination.index(anchor) + offset
    destination.insert(index, task_id)
    return TaskOrder(
        version=order.version + 1,
        sequences={status: tuple(ids) for status, ids in sequences.items()},
    )


def remove_id(order: TaskOrder, task_id: str) -> TaskOrder:
    """The order without ``task_id``, at ``version + 1``."""
    return TaskOrder(
        version=order.version + 1,
        sequences={
            status: tuple(i for i in order.sequences.get(status, ()) if i != task_id)
            for status in STATUSES
        },
    )


def flatten(order: TaskOrder) -> list[str]:
    """Every id: status order, then sequence order."""
    return [task_id for status in STATUSES for task_id in order.sequences.get(status, ())]
